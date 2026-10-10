import json
import time

import httpx

from . import config

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


class LLMParseError(LLMError):
    pass


class _Retryable(Exception):
    pass


def _request(payload):
    headers = {
        "Authorization": f"Bearer {config.ZHIPU_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(connect=10, read=config.CFG["llm"]["read_timeout_s"], write=30, pool=10)
    with httpx.Client(timeout=timeout) as client:
        try:
            resp = client.post(config.CFG["llm"]["base_url"], headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise _Retryable(str(e)) from e
    if resp.status_code in RETRYABLE_STATUS:
        raise _Retryable(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise _Retryable(f"响应结构异常: {json.dumps(data, ensure_ascii=False)[:200]}") from e
    return content, data.get("usage") or {}


def _log(task, messages, content, usage, ok, err):
    rec = {
        "time": config.now_iso(),
        "task": task,
        "model": config.CFG["llm"]["model"],
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


def _call(messages, task):
    payload = {
        "model": config.CFG["llm"]["model"],
        "messages": messages,
        "temperature": config.CFG["temperature"].get(task, 0.2),
    }
    # 本项目任务均为结构化提取/判定，关闭 GLM 深度思考以免长输入时思考耗时顶爆读超时
    if config.CFG["llm"].get("disable_thinking", True):
        payload["thinking"] = {"type": "disabled"}
    max_retries = config.CFG["llm"]["max_retries"]
    last = None
    for attempt in range(max_retries + 1):
        try:
            content, usage = _request(payload)
            _log(task, messages, content, usage, True, None)
            return content
        except _Retryable as e:
            last = e
            _log(task, messages, "", {}, False, str(e))
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


def chat(messages, *, task, response_json=False):
    if not config.ZHIPU_API_KEY:
        raise LLMError("缺少 ZHIPU_API_KEY：请复制 .env.example 为 .env 并填入智谱 API Key")
    content = _call(messages, task)
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
    content2 = _call(fix_messages, task)
    try:
        return _extract_json(content2)
    except ValueError as e:
        raise LLMParseError(f"两次输出均无法解析为 JSON: {e}；原文前 500 字: {content2[:500]}") from e
