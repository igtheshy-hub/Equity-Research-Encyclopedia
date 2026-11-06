"""ask 索引直答分支自测：use_index 路由、选页为空兜底、UNANSWERABLE 回退、锚点重试。用法：python tests/test_ask_index.py"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import ask as ask_mod  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def fake_pages():
    def p(type_, slug, summary):
        return {"path": None, "type": type_, "slug": slug,
                "meta": {"title": slug, "aliases": [], "summary": summary, "sources": []},
                "body": f"{slug} 正文", "anchors": set(), "statements": 1}
    pages = [p("company", "比亚迪", "整车厂"), p("company", "智谱", "AI大模型"), p("industry", "智能汽车", "智驾产业链")]
    entries = [ask_mod.wiki.build_index_entry(x) for x in pages]
    return pages, entries


class FakeLLM:
    """按脚本应答。intent 与 select 同为 task=select，靠提示词关键词区分。answers 依次消费。"""

    def __init__(self, select_out, answers):
        self.select_out = select_out
        self.answers = list(answers)
        self.tasks = []
        self.prompts = []

    def __call__(self, messages, *, task, response_json=False):
        self.tasks.append(task)
        self.prompts.append(messages[0]["content"])
        content = messages[0]["content"]
        if "意图" in content[:20]:
            return {"intent": "chat", "title": "", "domain": "general"}
        if response_json:
            return self.select_out
        return self.answers.pop(0)


def run(script_answers, select_out, suggest=None):
    pages, entries = fake_pages()
    ask_mod._index_pages = lambda: (pages, entries)
    ask_mod.raw_file_index = lambda: {}
    calls = {"suggest": 0}

    def fake_suggest(question):
        calls["suggest"] += 1
        return suggest if suggest is not None else []
    ask_mod._suggest_raw_files = fake_suggest
    fake = FakeLLM(select_out, script_answers)
    ask_mod.llm.chat = fake
    return ask_mod.ask("知识库是否都是汽车行业相关？", propose=True), fake, calls


def test_use_index_route():
    print("use_index 路由：")
    result, fake, calls = run(["知识库共 3 页，其中智谱为 AI 大模型公司，其余均与汽车相关。"],
                              {"selected": [], "use_index": True, "reason": "问知识库整体构成"})
    check("kind", result["kind"], "answer")
    check("answer_md", result["answer_md"], "知识库共 3 页，其中智谱为 AI 大模型公司，其余均与汽车相关。")
    check("validation ok", result["validation"]["ok"], True)
    check("selected 为空", result["step1"]["selected"], [])
    check("用了索引提示词", any("--- 知识库索引开始 ---" in c for c in fake.prompts), True)
    check("头部带机械计数", any("（索引共 3 页：company 2、industry 1，另有来源摘要页未列入本索引）" in c for c in fake.prompts), True)
    check("不触发沉淀", "audit" in fake.tasks, False)
    check("不问 raw", calls["suggest"], 0)


def test_fallback_route():
    print("选页为空兜底：")
    result, fake, calls = run(["知识库以汽车行业为主。"],
                              {"selected": [], "use_index": False, "reason": "无匹配页面"})
    check("kind", result["kind"], "answer")
    check("answer_md", result["answer_md"], "知识库以汽车行业为主。")
    check("reason 改写", result["step1"]["reason"], "选页为空，改用知识库总索引直答")
    check("不问 raw", calls["suggest"], 0)


def test_fallback_beats_raw_candidates():
    print("选页为空先走索引、不看 raw 候选：")
    result, fake, calls = run(["知识库以汽车行业为主。"],
                              {"selected": [], "use_index": False, "reason": "无匹配页面"},
                              suggest=[{"file": "东吴证券_整车研报_20260921.pdf", "reason": "文件名含整车"}])
    check("kind", result["kind"], "answer")
    check("answer_md", result["answer_md"], "知识库以汽车行业为主。")
    check("raw 建议未被调用", calls["suggest"], 0)


def test_fallback_unanswerable():
    print("兜底也答不了走拒绝：")
    result, _, _ = run(["UNANSWERABLE"], {"selected": [], "use_index": False, "reason": "无匹配页面"})
    check("kind", result["kind"], "referral")
    check("answer_md", result["answer_md"], ask_mod.REFERRAL_NONE)
    check("candidates", result["candidates"], [])


def test_use_index_unanswerable():
    print("索引直答答不了不再重复试、走拒绝：")
    result, fake, _ = run(["UNANSWERABLE"], {"selected": [], "use_index": True, "reason": "问库整体"})
    check("kind", result["kind"], "referral")
    check("answer_md", result["answer_md"], ask_mod.REFERRAL_NONE)
    check("answer 调用只一次", fake.tasks.count("answer"), 1)


def test_anchor_retry():
    print("索引答案带锚点触发重试：")
    result, fake, _ = run(["有 3 页 [[x.pdf#p1]]", "知识库共 3 页，均可在索引中看出。"],
                          {"selected": [], "use_index": True, "reason": "问库整体"})
    check("重试后通过", result["validation"]["ok"], True)
    check("answer_md 取修正版", result["answer_md"], "知识库共 3 页，均可在索引中看出。")
    check("answer 调用两次", fake.tasks.count("answer"), 2)


def test_fin_block_reads_skill_and_matches_embedded():
    assert ask_mod._fin_block().strip() == ask_mod.FIN_ANALYSIS_BLOCK.strip()


def test_fin_block_reads_marker_and_falls_back_when_missing(tmp_path, monkeypatch):
    skill = tmp_path / "SKILL.md"
    skill.write_text("前言\n<!-- ASK-RUNTIME:START -->\n新纪律\n<!-- ASK-RUNTIME:END -->\n",
                     encoding="utf-8")
    monkeypatch.setattr(ask_mod, "FIN_SKILL_PATH", skill)
    assert ask_mod._fin_block() == "\n新纪律\n"
    monkeypatch.setattr(ask_mod, "FIN_SKILL_PATH", tmp_path / "missing.md")
    assert ask_mod._fin_block() == ask_mod.FIN_ANALYSIS_BLOCK


def test_method_block_earnings_comes_from_earnings_review_skill():
    text = ask_mod._method_block("earnings")
    assert "业绩点评类问题按以下顺序组织答案" in text
    assert ask_mod._method_block("none") == ""


if __name__ == "__main__":
    test_use_index_route()
    test_fallback_route()
    test_fallback_beats_raw_candidates()
    test_fallback_unanswerable()
    test_use_index_unanswerable()
    test_anchor_retry()
    test_fin_block_reads_skill_and_matches_embedded()
    test_method_block_earnings_comes_from_earnings_review_skill()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
