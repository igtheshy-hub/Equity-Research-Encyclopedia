"""问题 15 自测：AI 把约定的 JSON「对象」交成「清单」时，统一出口要拦住并重试，
不再让 list 流到调用方 .get 处崩溃；归并规划约定本来就是数组，放行不受影响。

用法：python tests/test_json_format.py
全部在内存/临时目录里跑，不联网、不碰真实数据。
"""
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import config, llm  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


FAKE_PROF = {"id": "t", "model": "t", "key": "k", "key_env": "K", "max_tokens": 100,
             "read_timeout_s": 5, "max_retries": 1, "extra_body": {}, "base_url": "http://x"}

ARRAY = '["行1 [[a.pdf#p1]]", "行2 [[a.pdf#p2]]"]'
OBJ = '{"new_knowledge": true, "topic": "测试", "lines": ["行1 [[a.pdf#p1]]"]}'


class Script:
    """按脚本逐次返回 _call 的内容，并记录每次收到的消息。"""

    def __init__(self, *contents):
        self.contents = list(contents)
        self.calls = []

    def __call__(self, messages, task, prof):
        self.calls.append([dict(m) for m in messages])
        return self.contents[min(len(self.calls), len(self.contents)) - 1]


def main():
    real_profile = config.model_profile
    real_call = llm._call
    config.model_profile = lambda name=None: dict(FAKE_PROF)
    try:
        print("统一出口 chat(response_json=True)：")
        s = Script(ARRAY, OBJ)
        llm._call = s
        out = llm.chat([{"role": "user", "content": "q"}], task="audit", response_json=True)
        check("数组后重试拿到对象", (isinstance(out, dict), out.get("new_knowledge")), (True, True))
        check("重试只多调一次", len(s.calls), 2)
        fix_text = s.calls[1][-1]["content"]
        check("重试话术点名要对象", ("JSON 对象" in fix_text, "不要 JSON 数组" in fix_text), (True, True))

        s = Script(ARRAY, ARRAY)
        llm._call = s
        try:
            llm.chat([{"role": "user", "content": "q"}], task="audit", response_json=True)
            check("两次数组应报错", "没有抛异常", "LLMParseError")
        except llm.LLMParseError as e:
            check("两次数组抛 LLMParseError", "LLMParseError" in type(e).__name__, True)
        except Exception as e:  # noqa: BLE001
            check("两次数组抛 LLMParseError", f"抛了 {type(e).__name__}", "LLMParseError")
        check("报错可被 except llm.LLMError 接住", issubclass(llm.LLMParseError, llm.LLMError), True)

        s = Script("抱歉，我不知道。", OBJ)
        llm._call = s
        out = llm.chat([{"role": "user", "content": "q"}], task="audit", response_json=True)
        check("纯文字后重试拿到对象", isinstance(out, dict), True)

        s = Script(OBJ)
        llm._call = s
        out = llm.chat([{"role": "user", "content": "q"}], task="audit", response_json=True)
        check("对象一次放行", (isinstance(out, dict), len(s.calls)), (True, 1))

        s = Script(ARRAY)
        llm._call = s
        out = llm.chat([{"role": "user", "content": "q"}], task="audit", response_json=True, json_any=True)
        check("json_any 数组原样放行", (isinstance(out, list), out, len(s.calls)), (True, ["行1 [[a.pdf#p1]]", "行2 [[a.pdf#p2]]"], 1))

        # ---- 与问答流程集成：模型一直返回数组，各环节要优雅降级而不是崩 ----
        print("问答各环节一直拿到清单时：")
        always_array = Script(ARRAY, ARRAY, ARRAY)
        llm._call = always_array
        from src import ask as ask_mod
        check("意图判别回退默认", ask_mod._detect_intent("测试问题"), ("chat", "", "general"))
        check("沉淀提案返回 None 不崩", ask_mod._propose_distill("问题", "答案 [[a.pdf#p1]]"), None)
        check("锚点修复返回空表不崩", ask_mod._repair_anchors(["行 [[a.pdf#p1]]"], [("a.pdf", "p1")]), [])

        # ---- plan_merge：约定本来就是数组，json_any 放行 ----
        print("归并规划（约定就是数组）：")
        s = Script('[{"title": "新主题", "action": "create", "slug": "xin-zhuti", "type": "concept"}]')
        llm._call = s
        from src import ingest as ingest_mod
        topics = [{"title": "新主题", "type": "concept", "aliases": []}]
        index_entries = [{"title": "已有页", "slug": "s1", "type": "concept", "aliases": []}]
        decisions = ingest_mod.plan_merge(topics, index_entries)
        actions = [(d["action"], t["title"]) for d, t in decisions]
        check("数组决策被正常解析", ("create", "新主题") in actions, True)
        check("归并路径确实调了 LLM（否则测的是空索引捷径）", len(s.calls) >= 1, True)
    finally:
        config.model_profile = real_profile
        llm._call = real_call

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
