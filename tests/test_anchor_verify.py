"""出处锚点校验与抽查自测（对应问题 6：页码可能是编的，系统查不出来）。

两件事：
1) 范围校验收紧：锚点必须落在「本次真的给模型看过的那些单元」之内。
   旧逻辑只查「页码没超出文件页数」，AI 记混页号或编个在范围内的页号都能过。
2) 文本抽查：答案里的关键数字在锚点页原文里找不到的，记进审校清单待人工复核。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import anchor_verify, ask as ask_mod, wiki  # noqa: E402
from _common import check, run_tests, build_sandbox, REPORT_NAME  # noqa: E402

# 沙盒里的研报：第 1 页有 "Industry volume growth 12% YoY"，第 2 页有 "Top player share 45%"
# 只展示过第 1 页 → 第 2 页虽然在这个文件的页数范围内，但模型没见过，不许引
ALLOWED_SHOWN = {REPORT_NAME: ("page", 2, {"p1"})}
ALLOWED_WHOLE = {REPORT_NAME: ("page", 2)}          # 旧形态：只给总页数


def test_scope_tightened():
    print("校验范围收紧到「真的给模型看过的单元」：")
    # 页码在文件范围内（p2 <= 2），但这一页没喂给模型 → 必须拦下
    bad = ask_mod._violations(f"营收 100 亿元 [[{REPORT_NAME}#p2]]", ALLOWED_SHOWN)
    check("未展示过的单元被判违规", len(bad), 1)
    check("违规文案点明原因", "没有出现在本次提供给模型的原文里" in bad[0], True)
    # 喂过的单元 → 放行
    ok = ask_mod._violations(f"营收 100 亿元 [[{REPORT_NAME}#p1]]", ALLOWED_SHOWN)
    check("展示过的单元放行", ok, [])
    # 旧形态（只有总页数）仍按范围校验，行为不变
    old = ask_mod._violations(f"营收 100 亿元 [[{REPORT_NAME}#p1]]", ALLOWED_WHOLE)
    check("旧形态不受影响", old, [])
    out = ask_mod._violations(f"营收 100 亿元 [[{REPORT_NAME}#p9]]", ALLOWED_WHOLE)
    check("旧形态仍查越界", len(out), 1)


def test_allowed_from_pages():
    print("问答路径：只认选中知识页里真的出现过的锚点：")
    build_sandbox()
    pages = wiki.load_pages()
    ind = next(p for p in pages if p["slug"] == "汽车")
    allowed = ask_mod._allowed_from_pages([ind])
    check("研报在允许清单里", REPORT_NAME in allowed, True)
    check("展示过的锚点被记下", sorted(allowed[REPORT_NAME][2]), ["p1", "p2"])
    check("页面上有的锚点放行", ask_mod._violations(f"营收 100 亿元 [[{REPORT_NAME}#p1]]", allowed), [])


def test_text_spotcheck():
    print("文本抽查：关键数字在锚点页原文里找不找得到：")
    build_sandbox()
    # 12% 确实写在第 2 页 → 不该报
    check("页面上有的数字不报", anchor_verify.check(f"行业销量增长 12% [[{REPORT_NAME}#p2]]"), [])
    # 87.3% 只在第 1 页，第 2 页没有 → 报存疑
    bad = anchor_verify.check(f"行业销量增长 87.3% [[{REPORT_NAME}#p2]]")
    check("页面上没有的数字报存疑", len(bad), 1)
    check("存疑项带上文件名与页号", (bad[0]["file"], bad[0]["loc"]), (REPORT_NAME, "p2"))
    check("存疑项写明原因", "找不到" in bad[0]["reason"], True)


def test_doubts_land_in_audit_list():
    print("存疑项进审校清单：")
    build_sandbox()
    anchor_verify.append_doubts(
        anchor_verify.check(f"行业销量增长 87.3% [[{REPORT_NAME}#p2]]"),
        question="行业销量", answer="行业销量增长 87.3%")
    items = anchor_verify.load_doubts()
    check("清单里多了一条", len(items), 1)
    check("清单项带原句", "87.3%" in items[0]["sentence"], True)
    # 审校报告里要有这一节
    from src import audit as audit_mod
    import src.llm as llm_mod
    llm_mod.chat = lambda *a, **k: {"conflicts": [], "gaps": []}
    r = audit_mod.run()
    check("审校报告含锚点存疑一节", "## 锚点存疑" in r["path"].read_text(encoding="utf-8"), True)
    check("计数里带锚点存疑", r["counts"].get("锚点存疑", 0) >= 1, True)


def test_multi_anchor_attribution():
    print("多锚点句子按段归因，不把 A 段数字拿到 B 页找：")
    build_sandbox()
    # 12% 只在第 2 页；后半句没有数字。旧逻辑把 12% 拿去第 1 页找 → 假存疑
    a = f"行业销量增长 12% [[{REPORT_NAME}#p2]]；行业景气度持续回升 [[{REPORT_NAME}#p1]]"
    check("各段只核自己的锚点", anchor_verify.check(a), [])
    # 反向仍要抓得住：45% 标了第 1 页的锚点，但 45% 只在第 2 页 → 该报
    b = f"龙头份额 45% [[{REPORT_NAME}#p1]]；行业销量增长 12% [[{REPORT_NAME}#p2]]"
    bad = anchor_verify.check(b)
    check("张冠李戴的数字仍被揪出", len(bad), 1)
    check("存疑记在错误归属的那页", (bad[0]["file"], bad[0]["loc"]), (REPORT_NAME, "p1"))


def test_anchor_filename_not_a_number():
    print("锚点文件名里的日期不再被当成待核数字：")
    build_sandbox()
    # 句子本身没有数字 → 旧逻辑回退抓「有效位最多的数」，抓到文件名里的 20260101
    a = f"自研架构迭代多代，产品覆盖高中低端 [[{REPORT_NAME}#p1]]"
    check("文件名日期不进抽查", anchor_verify.check(a), [])


def test_consecutive_anchors_share_text():
    print("连排锚点共撑同一段，任一页找到即放行：")
    build_sandbox()
    # 12% 在第 2 页不在第 1 页，两个锚点连排标注同一句话
    a = f"行业销量增长 12% [[{REPORT_NAME}#p1]][[{REPORT_NAME}#p2]]"
    check("连排锚点按并集放行", anchor_verify.check(a), [])


def test_year_not_spotchecked():
    print("裸年份不算值得抽查的关键数字：")
    build_sandbox()
    a = f"下一代产品预计2027年量产 [[{REPORT_NAME}#p2]]"
    check("裸年份不报存疑", anchor_verify.check(a), [])


if __name__ == "__main__":
    run_tests(globals(), ["test_scope_tightened", "test_allowed_from_pages",
                          "test_text_spotcheck", "test_doubts_land_in_audit_list",
                          "test_multi_anchor_attribution", "test_anchor_filename_not_a_number",
                          "test_consecutive_anchors_share_text", "test_year_not_spotchecked"])
