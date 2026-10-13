"""_propose_distill 自测：锚点过滤、缺锚点补挂修复轮、兜底。用法：python tests/test_distill.py"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import ask as ask_mod  # noqa: E402

FAILS = []

ANSWER = "比亚迪归母净利润下滑18.97% [[比亚迪财报_20260300.pdf#p16]]，标准FCF为-976.72亿元 [[比亚迪财报_20260300.pdf#p11]]。"
GOOD_LINE = "【观点】2025FY 比亚迪增收不增利 [[比亚迪财报_20260300.pdf#p16]]"


class FakeLLM:
    """按提示词关键词区分 distill 与补锚点两类 audit 调用，依次消费脚本。"""

    def __init__(self, distill_out, repair_out=None):
        self.distill_out = distill_out
        self.repair_out = repair_out
        self.prompts = []

    def __call__(self, messages, *, task, response_json=False):
        self.prompts.append(messages[0]["content"])
        if "缺少溯源锚点" in messages[0]["content"]:
            return self.repair_out
        return self.distill_out


def setup(fake):
    ask_mod.llm.chat = fake
    ask_mod.wiki.load_pages = lambda: []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def audit_calls(fake):
    return sum(1 for c in fake.prompts if "值得沉淀进知识库" in c or "缺少溯源锚点" in c)


def test_repair_round():
    print("缺锚点触发补挂修复轮：")
    fake = FakeLLM(
        {"new_knowledge": True, "topic": "比亚迪", "type": "company", "summary": "增收不增利",
         "lines": ["【观点】2025FY 比亚迪增收不增利", "【观点】2025FY 比亚迪自由现金流大额失血"]},
        repair_out={"lines": [GOOD_LINE, "【观点】2025FY 比亚迪自由现金流大额失血 [[比亚迪财报_20260300.pdf#p11]]"]},
    )
    setup(fake)
    p = ask_mod._propose_distill("比亚迪2025年的年报怎么样", ANSWER)
    check("提案非空", p is not None, True)
    check("修复后行数", len(p["lines"]) if p else 0, 2)
    check("每行带锚点", all(ask_mod.wiki.ANCHOR_RE.findall(ln) for ln in p["lines"]), True)
    check("audit 共两次（判定+修复）", audit_calls(fake), 2)
    check("修复清单来自答案锚点", "比亚迪财报_20260300.pdf#p16" in fake.prompts[1], True)


def test_lines_already_anchored():
    print("行已带合法锚点不走修复：")
    fake = FakeLLM(
        {"new_knowledge": True, "topic": "比亚迪", "type": "company", "summary": "增收不增利",
         "lines": [GOOD_LINE]},
    )
    setup(fake)
    p = ask_mod._propose_distill("比亚迪2025年的年报怎么样", ANSWER)
    check("提案非空", p is not None, True)
    check("audit 只一次", audit_calls(fake), 1)


def test_repair_hallucinated_anchor():
    print("修复轮新造锚点仍被过滤、提案为空：")
    fake = FakeLLM(
        {"new_knowledge": True, "topic": "比亚迪", "type": "company", "summary": "增收不增利",
         "lines": ["【观点】2025FY 比亚迪增收不增利"]},
        repair_out={"lines": ["【观点】2025FY 比亚迪增收不增利 [[比亚迪财报_20260300.pdf#p99]]"]},
    )
    setup(fake)
    p = ask_mod._propose_distill("比亚迪2025年的年报怎么样", ANSWER)
    check("提案为空", p is None, True)


def test_not_new_knowledge():
    print("非新知识直接空提案：")
    fake = FakeLLM({"new_knowledge": False, "topic": "", "type": "concept", "summary": "", "lines": []})
    setup(fake)
    check("提案为空", ask_mod._propose_distill("比亚迪2025年的年报怎么样", ANSWER) is None, True)
    check("不触发修复", audit_calls(fake), 1)


def test_answer_without_anchors():
    print("答案本身无锚点不修复、提案为空：")
    fake = FakeLLM(
        {"new_knowledge": True, "topic": "比亚迪", "type": "company", "summary": "增收不增利",
         "lines": ["【观点】2025FY 比亚迪增收不增利"]},
    )
    setup(fake)
    check("提案为空", ask_mod._propose_distill("比亚迪2025年的年报怎么样", "答案没有锚点") is None, True)
    check("不触发修复", audit_calls(fake), 1)


if __name__ == "__main__":
    test_repair_round()
    test_lines_already_anchored()
    test_repair_hallucinated_anchor()
    test_not_new_knowledge()
    test_answer_without_anchors()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
