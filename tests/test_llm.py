"""src/llm.py 的测试。

之前所有涉及 LLM 的测试都把 llm.chat 换成假货，所以这一层（响应解析、JSON 抽取、
重试退避、模型切换的并发隔离）从来没被执行过——它恰恰是"模型返回格式不对时会不会
整条链路崩掉"的地方。

这里全部用假响应驱动，不联网。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import check, run_tests  # noqa: E402


# ── JSON 抽取 ───────────────────────────────────────────────────────────────
def test_extract_json():
    """从模型回复里抠 JSON。"""
    print("llm：JSON 抽取")
    from src.llm import _extract_json

    check("裸对象", _extract_json('{"a": 1}'), {"a": 1})
    check("带前后废话", _extract_json('好的，结果如下：{"a": 1} 以上。'), {"a": 1})
    check("数组", _extract_json('[1, 2, 3]'), [1, 2, 3])
    check("嵌套", _extract_json('{"a": {"b": [1, {"c": 2}]}}'), {"a": {"b": [1, {"c": 2}]}})
    check("字符串里有花括号", _extract_json('{"a": "}{"}'), {"a": "}{"})

    for bad, label in [("完全不是 JSON", "无 JSON 结构"),
                       ('{"a": ', "结构不完整")]:
        try:
            _extract_json(bad)
            check(f"{label} 报错", "没报错", "ValueError")
        except ValueError:
            check(f"{label} 报错", "ValueError", "ValueError")


def test_extract_json_keeps_outermost():
    """取的是最外层结构，不该把内层单独抠出来。"""
    print("llm：JSON 抽取取最外层")
    from src.llm import _extract_json

    got = _extract_json('前缀 {"outer": {"inner": 1}} 后缀')
    check("拿到最外层", got, {"outer": {"inner": 1}})


# ── 响应解析 ───────────────────────────────────────────────────────────────
def test_parse_reply_openai_format():
    """OpenAI 兼容格式（智谱 / DeepSeek / MiniMax）。"""
    print("llm：OpenAI 兼容响应解析")
    from src.llm import _parse_reply

    data = {"choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    text, usage = _parse_reply(data)
    check("取到正文", text, "你好")
    check("取到 usage", usage["prompt_tokens"], 10)


def test_parse_reply_anthropic_format():
    """Anthropic 格式：content 是块数组。"""
    print("llm：Anthropic 响应解析")
    from src.llm import _parse_reply

    data = {"content": [{"type": "text", "text": "答案"},
                        {"type": "thinking", "thinking": "思维过程不取"}],
            "usage": {"input_tokens": 8, "output_tokens": 3},
            "stop_reason": "end_turn"}
    text, usage = _parse_reply(data)
    check("只取 text 块", text, "答案")
    check("usage 字段映射", usage["completion_tokens"], 3)


def test_parse_reply_empty_is_retryable():
    """空正文要抛可重试异常（而不是当成正常回答）。"""
    print("llm：空响应判为可重试")
    from src.llm import _Retryable, _parse_reply

    try:
        _parse_reply({"choices": [{"message": {"content": "  "}, "finish_reason": "stop"}]})
        check("空响应抛异常", "没抛", "_Retryable")
    except _Retryable:
        check("空响应抛异常", "_Retryable", "_Retryable")


def test_parse_reply_bad_shape_is_retryable():
    """响应结构不对要抛可重试异常。"""
    print("llm：坏响应结构判为可重试")
    from src.llm import _Retryable, _parse_reply

    try:
        _parse_reply({"unexpected": "shape"})
        check("坏结构抛异常", "没抛", "_Retryable")
    except _Retryable:
        check("坏结构抛异常", "_Retryable", "_Retryable")


def test_is_anthropic():
    """按端点后缀认服务商格式。"""
    print("llm：服务商格式判定")
    from src.llm import _is_anthropic

    check("messages 结尾是 Anthropic", _is_anthropic("https://x/v1/messages"), True)
    check("带斜杠也是", _is_anthropic("https://x/v1/messages/"), True)
    check("chat/completions 不是", _is_anthropic("https://x/v1/chat/completions"), False)


def test_merge_adjacent():
    """相邻同角色消息要合并（Anthropic 要求 role 交替）。"""
    print("llm：相邻同角色合并")
    from src.llm import _merge_adjacent

    got = _merge_adjacent([
        {"role": "user", "content": "甲"},
        {"role": "user", "content": "乙"},
        {"role": "assistant", "content": "丙"},
    ])
    check("合并成两条", len(got), 2)
    check("合并后内容", got[0]["content"], "甲\n\n乙")
    check("空输入不炸", _merge_adjacent([]), [{"role": "user", "content": ""}])


# ── 模型切换 ───────────────────────────────────────────────────────────────
def test_use_model_context():
    """use_model 是上下文管理器，退出后恢复。"""
    print("llm：模型切换作用域")
    from src import llm

    before = llm.active_model()
    with llm.use_model("nonexistent-model-id"):
        pass  # 未知的模型 id 也不该把作用域搞坏
    check("退出后恢复", llm.active_model(), before)


def test_use_model_isolated_across_threads():
    """两个线程各用各的模型，不能互相污染（ContextVar 按线程隔离）。"""
    print("llm：模型切换跨线程隔离")
    from src import llm

    results = {}
    errors = []

    def worker(name, key):
        try:
            with llm.use_model(name):
                results[key] = llm._MODEL.get()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=worker, args=("m-a", "a")),
          threading.Thread(target=worker, args=("m-b", "b"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    check("没有报错", errors, [])
    check("A 线程拿到 A 模型", results.get("a"), "m-a")
    check("B 线程拿到 B 模型", results.get("b"), "m-b")


def test_active_model_uses_context():
    """active_model 要读当前上下文里的模型，而不是全局变量。"""
    print("llm：active_model 读上下文")
    from src import llm

    default = llm.active_model()
    with llm.use_model(default):
        check("作用域内是默认模型", llm.active_model(), default)


# ── chat 的 JSON 重试 ──────────────────────────────────────────────────────
def test_chat_json_retry_path():
    """第一次返回的不是 JSON 时，应该重试一次再给结论。"""
    print("llm：JSON 解析失败会重试一次")
    from src import llm

    calls = []

    def fake_call(messages, task, prof):
        calls.append(messages)
        if len(calls) == 1:
            return "这不是 JSON，只是一段说明文字。"
        return '{"ok": true}'

    orig = llm._call
    llm._call = fake_call
    try:
        prof = {"key": "x", "key_env": "K", "model": "m", "base_url": "http://x/v1/chat/completions",
                "read_timeout_s": 1, "max_retries": 1, "max_tokens": 100, "extra_body": {}}
        orig_profile = llm.config.model_profile
        llm.config.model_profile = lambda *a, **k: prof
        try:
            got = llm.chat([{"role": "user", "content": "问"}], task="select", response_json=True)
            check("重试后拿到 JSON", got, {"ok": True})
            check("调了两次", len(calls), 2)
            check("第二次是带纠正提示的", "无法解析" in calls[1][-1]["content"], True)
        finally:
            llm.config.model_profile = orig_profile
    finally:
        llm._call = orig


def test_chat_json_twice_bad_raises_parse_error():
    """两次都解析不了，要抛 LLMParseError（带原文片段便于排查）。"""
    print("llm：两次都坏掉抛 LLMParseError")
    from src import llm

    def fake_call(messages, task, prof):
        return "依然不是 JSON"

    orig = llm._call
    llm._call = fake_call
    try:
        prof = {"key": "x", "key_env": "K", "model": "m", "base_url": "http://x/v1/chat/completions",
                "read_timeout_s": 1, "max_retries": 1, "max_tokens": 100, "extra_body": {}}
        orig_profile = llm.config.model_profile
        llm.config.model_profile = lambda *a, **k: prof
        try:
            llm.chat([{"role": "user", "content": "问"}], task="select", response_json=True)
            check("抛了解析错误", "没抛", "LLMParseError")
        except llm.LLMParseError as e:
            check("抛了解析错误", "LLMParseError", "LLMParseError")
            check("错误里带原文", "依然不是 JSON" in str(e), True)
        finally:
            llm.config.model_profile = orig_profile
    finally:
        llm._call = orig


def test_chat_plain_text_no_parse():
    """response_json=False 时不做 JSON 解析，原样返回文本。"""
    print("llm：纯文本模式不解析 JSON")
    from src import llm

    def fake_call(messages, task, prof):
        return "随便一段文字"

    orig = llm._call
    llm._call = fake_call
    try:
        prof = {"key": "x", "key_env": "K", "model": "m", "base_url": "http://x/v1/chat/completions",
                "read_timeout_s": 1, "max_retries": 1, "max_tokens": 100, "extra_body": {}}
        orig_profile = llm.config.model_profile
        llm.config.model_profile = lambda *a, **k: prof
        try:
            got = llm.chat([{"role": "user", "content": "问"}], task="select")
            check("原样返回", got, "随便一段文字")
        finally:
            llm.config.model_profile = orig_profile
    finally:
        llm._call = orig


def test_chat_missing_key_raises():
    """没配 key 时要给出清楚的报错，而不是发请求失败。"""
    print("llm：缺 key 报清楚的错")
    from src import llm

    orig_profile = llm.config.model_profile
    llm.config.model_profile = lambda *a, **k: {
        "key": "", "key_env": "SOME_KEY_ENV", "model": "m",
        "base_url": "http://x/v1/chat/completions", "read_timeout_s": 1,
        "max_retries": 1, "max_tokens": 100, "extra_body": {},
    }
    try:
        llm.chat([{"role": "user", "content": "问"}], task="select")
        check("抛了 LLMError", "没抛", "LLMError")
    except llm.LLMError as e:
        check("抛了 LLMError", "LLMError", "LLMError")
        check("报错里点名了缺哪个变量", "SOME_KEY_ENV" in str(e), True)
    finally:
        llm.config.model_profile = orig_profile


if __name__ == "__main__":
    run_tests(globals(), [
        "test_extract_json",
        "test_extract_json_keeps_outermost",
        "test_parse_reply_openai_format",
        "test_parse_reply_anthropic_format",
        "test_parse_reply_empty_is_retryable",
        "test_parse_reply_bad_shape_is_retryable",
        "test_is_anthropic",
        "test_merge_adjacent",
        "test_use_model_context",
        "test_use_model_isolated_across_threads",
        "test_active_model_uses_context",
        "test_chat_json_retry_path",
        "test_chat_json_twice_bad_raises_parse_error",
        "test_chat_plain_text_no_parse",
        "test_chat_missing_key_raises",
    ])
