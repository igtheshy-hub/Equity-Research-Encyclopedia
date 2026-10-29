"""A-F 候选解析与校验自测：无锚点句筛选、A/D/E 三类门槛、锚点原样匹配。

用法：python tests/test_af_parse.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src import report_distill  # noqa: E402

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


INFO = {"x.pdf": ("page", 10)}
REPORT_ANCHORS = {("x.pdf", "p5"), ("x.pdf", "p6")}


def cand(cls, **kw):
    base = {
        "class": cls,
        "judgment": "判断句",
        "anchors": [("x.pdf", "p5")],
        "condition": "",
        "forecast_date": "",
        "metric": "",
        "period": "",
        "value": "",
        "unit": "",
        "calc": "",
    }
    base.update(kw)
    return base


def main():
    print("== 无锚点句筛选 ==")
    md = (
        "# 标题\n\n"
        "## 一、投资要点\n\n"
        "行业需求正在从政策驱动转向消费驱动，这是跨材料综合得到的判断。\n\n"
        "有锚点的句子不参与分类 [[x.pdf#p5]]\n\n"
        "| 指标 | 数值 |\n| --- | --- |\n| 销量 | 100 |\n\n"
        "短句\n\n"
        "这是一句重复出现的无锚点判断句。\n\n"
        "这是一句重复出现的无锚点判断句。\n"
    )
    got = report_distill.blank_sentences(md)
    check("只留无锚点陈述行并去重", got,
          ["行业需求正在从政策驱动转向消费驱动，这是跨材料综合得到的判断。", "这是一句重复出现的无锚点判断句。"])

    print("== 锚点归一 ==")
    check("裸锚点归一", report_distill._norm_anchor("x.pdf#p5"), ("x.pdf", "p5"))
    check("带括号锚点归一", report_distill._norm_anchor("[[x.pdf#para3]]"), ("x.pdf", "para3"))
    check("不合法锚点拒绝", report_distill._norm_anchor("x.pdf#3"), None)
    check("非 pdf/docx 拒绝", report_distill._norm_anchor("x.txt#p5"), None)

    print("== 三类门槛 ==")
    ok, _ = report_distill.validate_candidate(cand("A"), REPORT_ANCHORS, INFO)
    check("A 类通过", ok, True)
    ok, reason = report_distill.validate_candidate(cand("D"), REPORT_ANCHORS, INFO)
    check("D 类缺条件拒绝", (ok, "预测条件" in reason), (False, True))
    ok, _ = report_distill.validate_candidate(
        cand("D", condition="若购置税优惠延续", forecast_date="2026-12"), REPORT_ANCHORS, INFO)
    check("D 类条件日期齐全通过", ok, True)
    ok, reason = report_distill.validate_candidate(cand("E", metric="销量", value="100", calc="口径"),
                                                   REPORT_ANCHORS, INFO)
    check("E 类报告期格式拒绝", (ok, "报告期" in reason), (False, True))
    ok, _ = report_distill.validate_candidate(
        cand("E", metric="销量", period="2024FY", value="100", unit="万辆", calc="年度累计"),
        REPORT_ANCHORS, INFO)
    check("E 类字段齐全通过", ok, True)
    ok, reason = report_distill.validate_candidate(cand("A", anchors=[("y.pdf", "p5")]),
                                                   REPORT_ANCHORS, INFO)
    check("报告里没有的锚点拒绝", (ok, "不在报告正文" in reason), (False, True))
    ok, reason = report_distill.validate_candidate(cand("A", anchors=[("x.pdf", "p9")]),
                                                   REPORT_ANCHORS, INFO)
    check("报告正文没有 p9 锚点拒绝", ok, False)
    ok, reason = report_distill.validate_candidate(cand("A", anchors=[]), REPORT_ANCHORS, INFO)
    check("没有锚点拒绝", (ok, "没有可用锚点" in reason), (False, True))

    print("== 沉淀行组装 ==")
    line = report_distill.build_distill_line(cand("A", judgment="行业集中度还会提升"),
                                             {"report_path": "outputs/report-x.md"})
    check("A 类行首", line.startswith("【个人观点】行业集中度还会提升"), True)
    check("A 类带锚点", "[[x.pdf#p5]]" in line, True)
    check("A 类带报告出处", "（个人研报观点，" in line and "report-x.md" in line, True)
    line = report_distill.build_distill_line(
        cand("D", judgment="销量会回落", condition="补贴退坡", forecast_date="2026-12"),
        {"report_path": "outputs/report-x.md"})
    check("D 类写清条件", "预测条件：补贴退坡" in line, True)
    check("D 类写清时点", "预测时点：2026-12" in line, True)
    line = report_distill.build_distill_line(
        cand("E", metric="销量", period="2024FY", value="100", unit="万辆", calc="年度累计"),
        {"report_path": "outputs/report-x.md"})
    check("E 类行首", line.startswith("【指标】2024FY 销量 = 100万辆"), True)
    check("E 类带口径", "年度累计" in line and "[[x.pdf#p5]]" in line, True)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项未通过：{'、'.join(FAILS)}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
