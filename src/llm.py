import contextlib
import contextvars
import json
import time

import httpx

from . import config

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 本次调用的模型档案覆盖（None = 用 config.json 的 llm.default）。
# 后台任务各跑在自己的线程里，ContextVar 按线程隔离，两个请求用不同模型不会互相串。
_MODEL = contextvars.ContextVar("llm_model", default=None)


@contextlib.contextmanager
def use_model(name):
    token = _MODEL.set(name or None)
    try:
        yield
    finally:
        _MODEL.reset(token)


def active_model():
    return config.model_profile(_MODEL.get())["model"]


class LLMError(RuntimeError):
    pass


class LLMParseError(LLMError):
    pass


class _Retryable(Exception):
    pass


def _is_anthropic(url):
    # Anthropic 格式端点以 /messages 结尾（火山方舟），OpenAI 兼容端点以 /chat/completions 结尾（智谱等）
    return url.rstrip("/").endswith("/messages")


def _parse_reply(data):
    """按响应结构认服务商格式：Anthropic 给 content 块数组，OpenAI 兼容给 choices。"""
    blocks = data.get("content")
    if isinstance(blocks, list):
        # 关思考后正常只有 text 块；thinking 块不取，避免把思维过程当正文
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        u = data.get("usage") or {}
        usage = {"prompt_tokens": u.get("input_tokens"), "completion_tokens": u.get("output_tokens")}
        stop = data.get("stop_reason")
    else:
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise _Retryable(f"响应结构异常: {json.dumps(data, ensure_ascii=False)[:200]}") from e
        u = data.get("usage") or {}
        usage = {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens")}
        stop = (data.get("choices") or [{}])[0].get("finish_reason")
    if not text.strip():
        raise _Retryable(f"响应无正文（stop_reason={stop}）: {json.dumps(data, ensure_ascii=False)[:200]}")
    return text, usage


def _request(payload, prof):
    url = prof["base_url"]
    headers = {
        "Authorization": f"Bearer {prof['key']}",
        "Content-Type": "application/json",
    }
    if _is_anthropic(url):
        headers["anthropic-version"] = "2023-06-01"
    timeout = httpx.Timeout(connect=10, read=prof["read_timeout_s"], write=30, pool=10)
    with httpx.Client(timeout=timeout) as client:
        try:
            resp = client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _Retryable(str(e)) from e
    if resp.status_code in RETRYABLE_STATUS:
        raise _Retryable(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return _parse_reply(resp.json())


def _log(task, messages, content, usage, ok, err, prof):
    rec = {
        "time": config.now_iso(),
        "task": task,
        "model": prof["model"],
        "in_chars": sum(len(m.get("content", "")) for m in messages),
        "out_chars": len(content),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "ok": ok,
        "error": err,
    }
    config.LLM_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with config.LLM_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _merge_adjacent(messages):
    """相邻同角色消息并成一条：Anthropic 格式要求 role 交替，OpenAI 兼容格式也不受影响。"""
    conv = []
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "")
        if conv and conv[-1]["role"] == role:
            conv[-1]["content"] += "\n\n" + text
        else:
            conv.append({"role": role, "content": text})
    return conv or [{"role": "user", "content": ""}]


def _call(messages, task, prof):
    payload = {
        "model": prof["model"],
        "max_tokens": prof["max_tokens"],
        "messages": _merge_adjacent(messages),
        "temperature": config.CFG["temperature"].get(task, 0.2),
    }
    # 各服务商的私有参数（如关深度思考 {"thinking": {"type": "disabled"}}）走 config.json 的 llm.extra_body，
    # 不把某家的字段硬编码进请求体，换服务商时只改配置
    payload.update(prof.get("extra_body") or {})
    max_retries = prof["max_retries"]
    last = None
    for attempt in range(max_retries + 1):
        try:
            content, usage = _request(payload, prof)
            _log(task, messages, content, usage, True, None, prof)
            return content
        except _Retryable as e:
            last = e
            _log(task, messages, "", {}, False, str(e), prof)
            if attempt < max_retries:
                time.sleep(2 ** (attempt + 1))
    raise LLMError(f"重试 {max_retries} 次后仍失败: {last}")


def _extract_json(text):
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        raise ValueError("未找到 JSON 结构")
    start = min(starts)
    end = max(text.rfind("}"), text.rfind("]"))
    if end <= start:
        raise ValueError("JSON 结构不完整")
    return json.loads(text[start:end + 1])


def chat(messages, *, task, response_json=False, model=None):
    prof = config.model_profile(model or _MODEL.get())
    if not prof["key"]:
        raise LLMError(f"缺少 {prof['key_env']}：请复制 .env.example 为 .env 并填入模型服务商的 API Key")
    content = _call(messages, task, prof)
    if not response_json:
        return content
    try:
        return _extract_json(content)
    except ValueError:
        pass
    fix_messages = messages + [{
        "role": "user",
        "content": "上一条输出无法解析为合法 JSON。重新输出：只给合法 JSON 本体，不要解释，不要 Markdown 围栏。",
    }]
    content2 = _call(fix_messages, task, prof)
    try:
        return _extract_json(content2)
    except ValueError as e:
        raise LLMParseError(f"两次输出均无法解析为 JSON: {e}；原文前 500 字: {content2[:500]}") from e
