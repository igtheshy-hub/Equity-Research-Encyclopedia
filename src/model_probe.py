"""模型接入探活：网页添加模型前分别验证两条协议是否真的能用。

主流程（问答、摄入、成文）走 OpenAI 兼容的 chat/completions；
救火员 Claude 子进程走 Anthropic 兼容的 /v1/messages。
两个端点的路径和鉴权头不同，各发一条最小请求，失败时把服务商返回的原因带回来，
让配置错误在添加模型时当场报出来，而不是等到半夜救火才发现。
"""
import json
import urllib.error
import urllib.request


def _post_json(url, headers, payload, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, e.code, e.read(4000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - 网络错误统一转成一句人话
        return False, 0, f"{type(e).__name__}: {e}"


def _short_error(body):
    """从服务商返回里抽一句人能看懂的原因，截断防止刷屏。"""
    try:
        data = json.loads(body)
    except ValueError:
        return (body or "").strip()[:200] or "无返回内容"
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or err.get("type")
        elif err:
            msg = str(err)
        else:
            msg = data.get("message") or data.get("msg")
        if msg:
            return str(msg)[:200]
    return (body or "").strip()[:200] or "无返回内容"


def probe_chat(base_url, api_key, model, timeout=60):
    """验证主流程端点：OpenAI 兼容 chat/completions。"""
    ok, status, body = _post_json(
        base_url,
        {"content-type": "application/json",
         "authorization": f"Bearer {api_key}"},
        {"model": model,
         "messages": [{"role": "user", "content": "ping"}],
         "max_tokens": 16, "stream": False},
        timeout)
    if ok:
        return True, "chat 协议可用"
    return False, f"chat 协议失败（HTTP {status}）：{_short_error(body)}"


def probe_anthropic(base_url, api_key, model, timeout=60):
    """验证救火端点：Anthropic 兼容 /v1/messages。"""
    url = base_url.rstrip("/") + "/v1/messages"
    ok, status, body = _post_json(
        url,
        {"content-type": "application/json",
         "x-api-key": api_key,
         "authorization": f"Bearer {api_key}",
         "anthropic-version": "2023-06-01"},
        {"model": model, "max_tokens": 16,
         "messages": [{"role": "user", "content": "ping"}]},
        timeout)
    if ok:
        return True, "救火端点可用"
    return False, f"救火端点失败（HTTP {status}）：{_short_error(body)}"
