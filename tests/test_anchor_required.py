"""缺锚点检查与重试自测：模型整篇漏挂锚点时须触发既有重试。用法：python tests/test_anchor_required.py"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import ask as ask_mod  # noqa: E402

FAILS = []
FILE = "比亚迪财报_20260300.pdf"
ALLOWED = {FILE: ("page", 200)}

NO_ANCHOR_ANSWER = ("比亚迪2025年营收8039.65亿元，同比增长3.46%；归母净利润326.19亿元，"
                    "同比下降18.97%，核心营业利润率3.16%，呈现增收不增利特征。")
ANCHORED_ANSWER = f"比亚迪2025年营收8039.65亿元 [[{FILE}#p11]]，归母净利润326.19亿元 [[{FILE}#p16]]。"


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def test_missing_anchor_flagged():
    print("整篇漏锚点判违规：")
    check("零锚点数据答案", ask_mod._violations(NO_ANCHOR_ANSWER, ALLOWED),
          ["全文没有任何锚点，含数据的陈述句句尾必须带锚点"])
    check("带锚点不判", ask_mod._violations(ANCHORED_ANSWER, ALLOWED), [])


def test_no_false_positive():
    print("不误伤的场景：")
    check("无锚点可用时放过", ask_mod._violations(NO_ANCHOR_ANSWER, {}), [])
    check("无数字的说明答复", ask_mod._violations("页面介绍了比亚迪的主营业务与治理结构。", ALLOWED), [])
    check("短答不足三个数字", ask_mod._violations("2025年营收增长3.46%。", ALLOWED), [])


def test_existing_checks_kept():
    print("原有锚点校验不受影响：")
    check("引用未提供的文件", ask_mod._violations("营收8039.65亿元 [[别的文件.pdf#p1]]。", ALLOWED),
          ["锚点引用了未提供的文件 别的文件.pdf"])
    check("序号超范围", ask_mod._violations(f"营收8039.65亿元 [[{FILE}#p999]]。", ALLOWED),
          [f"锚点 {FILE}#p999 序号超出范围"])


def test_gap_plus_anchor_allowed():
    print("个别项无法核验、其余带锚点不判违规：")
    mixed = (f"营收8039.65亿元 [[{FILE}#p11]]，归母净利润326.19亿元 [[{FILE}#p16]]。\n"
             f"- 毛利率偏离同业：知识库中没有同业数据，无法核验。\n"
             f"- 待查项：高管与会计师更换等六大警讯属库外信息，知识库中没有该信息。")
    check("混写不判违规", ask_mod._violations(mixed, ALLOWED), [])


class FakeLLM:
    """intent/select 走 response_json，answer 依次消费脚本。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.tasks = []

    def __call__(self, messages, *, task, response_json=False):
        self.tasks.append(task)
        if "意图" in messages[0]["content"][:20]:
            return {"intent": "chat", "title": "", "domain": "general"}
        if response_json:
            return {"selected": ["比亚迪"], "use_index": False, "reason": "问比亚迪年报"}
        return self.answers.pop(0)


def run(answers):
    page = {"path": None, "type": "company", "slug": "比亚迪",
            "meta": {"title": "比亚迪", "aliases": [], "summary": "整车厂", "sources": [FILE]},
            "body": "【数据】2025FY 营业收入 = 803,964,958,000 元", "anchors": set(), "statements": 1}
    ask_mod._index_pages = lambda: ([page], [ask_mod.wiki.build_index_entry(page)])
    ask_mod.load_manifest = lambda: {"files": [{"file": FILE, "unit": "page", "count": 200}]}
    fake = FakeLLM(answers)
    ask_mod.llm.chat = fake
    return ask_mod.ask("比亚迪2025年年报怎么样", propose=False), fake


def test_retry_restores_anchors():
    print("零锚点答案触发重试：")
    result, fake = run([NO_ANCHOR_ANSWER, ANCHORED_ANSWER])
    check("取重试后版本", result["answer_md"], ANCHORED_ANSWER)
    check("校验通过", result["validation"]["ok"], True)
    check("answer 调用两次", fake.tasks.count("answer"), 2)


def test_no_retry_when_anchored():
    print("首答带锚点不重试：")
    result, fake = run([ANCHORED_ANSWER])
    check("答案即首答", result["answer_md"], ANCHORED_ANSWER)
    check("answer 只一次", fake.tasks.count("answer"), 1)


if __name__ == "__main__":
    test_missing_anchor_flagged()
    test_no_false_positive()
    test_existing_checks_kept()
    test_gap_plus_anchor_allowed()
    test_retry_restores_anchors()
    test_no_retry_when_anchored()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
