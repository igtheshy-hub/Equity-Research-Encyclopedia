"""anchor_verify 的数值匹配器语义测试。

问题 6 的修复把"字符串子串匹配"换成了"数值比对（含单位换算与舍入形态）"，
这套语义之前没有任何测试覆盖——改坏了不会亮红灯。这里把四种场景钉死：

  放行：原文 803,964,958,000 元  → 答案 8039.65 亿元   （单位换算）
  放行：原文 17.74%              → 答案 18%            （合法舍入）
  放行：原文 12,345,600 元        → 答案 1234.56 万元     （单位换算）
  存疑：原文 1450 万辆            → 答案 45 万辆         （子串误判的反面）
  存疑：原文 2.53 元/股           → 答案 2.55 元/股      （相近但不等）
  存疑：原文 12.34%               → 答案 99.9%           （完全不同）
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _common import check, run_tests  # noqa: E402


def _texts(body: str) -> dict:
    """构造 anchor_verify.check 需要的 {("文件名", 单元号): 原文} 字典。"""
    return {("A.pdf", 1): body}


def _passes(answer: str, page: str) -> bool:
    """答案是否被放行（没被判存疑）。"""
    from src.anchor_verify import check as anchor_check
    return len(anchor_check(answer, _texts(page))) == 0


def test_unit_conversion_passes():
    """元 → 亿元 的换算要放行：模型被要求换算，越守规矩越不能被误判。"""
    print("锚点抽查：单位换算不误伤")
    check("元→亿元",
          _passes("营收 8039.65 亿元。[[A.pdf#p1]]",
                  "营业收入 803,964,958,000 元，同比增长 12%。"), True)
    check("元→万元",
          _passes("营收 1234.56 万元。[[A.pdf#p1]]",
                  "营业收入 12,345,600 元。"), True)
    check("亿元→元（反向）",
          _passes("营收 803964958000 元。[[A.pdf#p1]]",
                  "营业收入 8039.65 亿元。"), True)


def test_legit_rounding_passes():
    """合法舍入要放行。"""
    print("锚点抽查：合法舍入不误伤")
    check("17.74% → 18%",
          _passes("毛利率 18%。[[A.pdf#p1]]", "毛利率 17.74%。"), True)
    check("1.42% → 1.4%",
          _passes("净息差 1.4%。[[A.pdf#p1]]", "净息差 1.42%。"), True)
    check("原值一致",
          _passes("营收 803964958000 元。[[A.pdf#p1]]",
                  "营业收入 803,964,958,000 元。"), True)


def test_fabricated_numbers_flagged():
    """编造的数字要存疑。"""
    print("锚点抽查：编造的数字能抓到")
    check("1450 万辆 → 45 万辆",
          _passes("销量 45 万辆。[[A.pdf#p1]]", "销量 1450 万辆。"), False)
    check("2.53 元/股 → 2.55 元/股",
          _passes("每股 2.55 元/股。[[A.pdf#p1]]", "每股 2.53 元/股。"), False)
    check("12.34% → 99.9%",
          _passes("占比 99.9%。[[A.pdf#p1]]", "占比 12.34%。"), False)
    check("45.2 天 → 46 天",
          _passes("周转 46 天。[[A.pdf#p1]]", "周转 45.2 天。"), False)


def test_all_numbers_in_sentence_checked():
    """一句里多个数字时要全核，不能只核最长的那个。"""
    print("锚点抽查：一句多个数字全核")
    from src.anchor_verify import check as anchor_check

    # 句里两个数字：一个在原文里、一个不在 → 应当存疑那一个
    ans = "营收 100 亿元，净利润 50 亿元。[[A.pdf#p1]]"
    d = anchor_check(ans, _texts("营业收入 100 亿元，净利润 60 亿元。"))
    check("抓到存疑项", len(d) >= 1, True)
    nums = sorted(x["number"] for x in d)
    check("存疑的是编造的那个 50", 50.0 in nums, True)
    check("没误伤正确的 100", 100.0 in nums, False)


def test_no_anchor_no_check():
    """没有锚点的句子不参与抽查。"""
    print("锚点抽查：无锚点不查")
    check("无锚点返回空",
          _passes("营收 1234.56 亿元。", "营业收入 803,964,958,000 元。"), True)


def test_missing_page_text_skipped():
    """拿不到原文时跳过（不误伤）。"""
    print("锚点抽查：缺原文跳过")
    from src.anchor_verify import check as anchor_check
    d = anchor_check("营收 1234.56 亿元。[[不存在.pdf#p1]]", {})
    check("缺原文不产生存疑", len(d), 0)


def test_doubt_reason_has_context():
    """存疑项要带上足够信息让人复核。"""
    print("锚点抽查：存疑项带上下文")
    from src.anchor_verify import check as anchor_check

    d = anchor_check("营收 9999 亿元。[[A.pdf#p1]]", _texts("营业收入 100 亿元。"))
    check("有一条存疑", len(d), 1)
    if d:
        item = d[0]
        check("带文件名", item["file"], "A.pdf")
        check("带定位", item["loc"], "p1")
        check("带数字", item["number"], 9999.0)
        check("带原句", "9999" in item["sentence"], True)
        check("带原因", "找不到" in item["reason"], True)


if __name__ == "__main__":
    run_tests(globals(), [
        "test_unit_conversion_passes",
        "test_legit_rounding_passes",
        "test_fabricated_numbers_flagged",
        "test_all_numbers_in_sentence_checked",
        "test_no_anchor_no_check",
        "test_missing_page_text_skipped",
        "test_doubt_reason_has_context",
    ])
