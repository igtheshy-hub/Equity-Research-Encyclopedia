"""引文解析与原文比对的单元测试。

整套系统的诚信底盘；这里没跑通 → 不要放其它测试通过绿色就上线。

锚点格式（与 schema/CLAUDE.md §1 / Igshy 主仓对齐）：
  [[file.pdf#p5]] 或 [[file.docx#para3]]
"""
from __future__ import annotations

import pytest

from app.services import citation as cite_svc
from app.services.citation import (
    ANCHOR_RE,
    WIKILINK_RE,
    CitationRef,
    FilenameResolver,
    _norm,
    extract_citations,
    loc_kind,
    loc_num,
    quote_in_page,
    split_into_sentences,
    verify_body_md,
)


# ---------------------------------------------------------------------------
# 正则与基础解析
# ---------------------------------------------------------------------------
def test_anchor_re_extracts_pdf():
    text = "营收同比+12.5%。[[招商证券_2026Q1策略.pdf#p5]]"
    matches = list(ANCHOR_RE.finditer(text))
    assert len(matches) == 1
    m = matches[0]
    assert m.group(1) == "招商证券_2026Q1策略.pdf"
    assert m.group(2) == "p5"


def test_anchor_re_extracts_docx():
    text = "财政部 2025 年财政赤字率 3%。[[财政部公告_20250315.docx#para3]]"
    matches = list(ANCHOR_RE.finditer(text))
    assert len(matches) == 1
    assert matches[0].group(1) == "财政部公告_20250315.docx"
    assert matches[0].group(2) == "para3"


def test_wikilink_re_extracts_page_title():
    text = "详见 [[贵州茅台]] 与 [[中信证券 白酒行业]] 的对比。"
    titles = [m.group(1).strip() for m in WIKILINK_RE.finditer(text)]
    assert "贵州茅台" in titles
    assert "中信证券 白酒行业" in titles


# ---------------------------------------------------------------------------
# loc_kind / loc_num 解析
# ---------------------------------------------------------------------------
def test_loc_kind_and_num():
    assert loc_kind("p3") == "page"
    assert loc_kind("para2") == "para"
    assert loc_num("p3") == 3
    assert loc_num("para2") == 2


# ---------------------------------------------------------------------------
# Token 提取
# ---------------------------------------------------------------------------
def test_extract_simple_token():
    md = "公司 2026 Q1 营收同比+12.5%。[[招商证券_2026Q1策略.pdf#p5]]"
    refs = extract_citations(md)
    assert len(refs) == 1
    r = refs[0]
    assert r.filename == "招商证券_2026Q1策略.pdf"
    assert r.page_no == 5
    assert "公司 2026 Q1 营收同比+12.5%" in r.preceding_text


def test_extract_multiple_tokens():
    md = (
        "分析师认为白酒行业进入新周期；高端化加速。[[招商证券_白酒周报.pdf#p3]]\n"
        "次高端表现分化。[[华泰证券_2026Q2展望.pdf#p8]]\n"
    )
    refs = extract_citations(md)
    assert len(refs) == 2
    assert refs[0].page_no == 3 and refs[1].page_no == 8


def test_extract_docx_token_with_para():
    md = "财政部公告近期赤字安排。[[财政部公告_20250315.docx#para3]]"
    refs = extract_citations(md)
    assert len(refs) == 1
    assert refs[0].para_no == 3
    assert refs[0].page_no is None


def test_extract_no_token_returns_empty():
    assert extract_citations("纯文本，没有任何 token。") == []


def test_extract_ignores_malformed():
    md = "[[xxx#abc]]"   # 不是 p\d+ 或 para\d+
    assert extract_citations(md) == []
    md2 = "[[xxx.pdf]]"   # 缺 #locator
    assert extract_citations(md2) == []


def test_extract_filenames_with_parens():
    md = "财务费用化低于预期。[[招商证券(上海)2026Q1报告.pdf#p9]]"
    refs = extract_citations(md)
    assert len(refs) == 1
    assert refs[0].filename == "招商证券(上海)2026Q1报告.pdf"
    assert refs[0].page_no == 9


# ---------------------------------------------------------------------------
# 文本归一化 & 模糊匹配
# ---------------------------------------------------------------------------
def test_norm_collapses_whitespace_and_fullwidth():
    s = "  公司   2026   Q1 \n 营收。 "
    assert _norm(s) == "公司 2026 Q1 营收."


def test_quote_in_page_strict_substring():
    page = "2026 年一季度，公司实现营业收入 123.4 亿元，同比增长 12.5%。"
    q = "公司实现营业收入 123.4 亿元"
    assert quote_in_page(page, q) == pytest.approx(1.0)


def test_quote_in_page_whitespace_tolerated():
    page = "公司实现营业收入\n  123.4 亿元，同比增长\n 12.5%。"
    q = "公司实现营业收入 123.4 亿元"
    assert quote_in_page(page, q) >= 0.85


def test_quote_in_page_partial_match_low_score():
    page = "今天天气很好。"
    q = "招商证券看好 2026 A 股"
    score = quote_in_page(page, q)
    assert score < 0.6


def test_quote_in_page_empty_returns_zero():
    assert quote_in_page("", "abc") == 0.0
    assert quote_in_page("abc", "") == 0.0


# ---------------------------------------------------------------------------
# 验证链路
# ---------------------------------------------------------------------------
def _resolver_with(rid: int, filename: str, pages: dict[str, str]) -> FilenameResolver:
    return FilenameResolver.build([(rid, filename, pages)])


def test_verify_body_md_ok():
    md = "公司预期 2026 年净利润 50 亿。[[aa.pdf#p5]]"
    res = _resolver_with(1, "aa.pdf", {
        "p5": "市场观点汇总。公司预期 2026 年净利润 50 亿，YoY+8%。"
    })
    res = verify_body_md(md, resolver=res)
    assert len(res) == 1 and res[0].verified is True
    assert res[0].report_id == 1


def test_verify_body_md_unknown_filename_marks_review():
    md = "公司预期净利润。[[未上传.pdf#p5]]"
    res = _resolver_with(1, "aa.pdf", {"p5": "公司预期净利润"})
    res = verify_body_md(md, resolver=res)
    assert len(res) == 1
    assert res[0].report_id is None
    assert res[0].needs_review is True
    assert any("文件名未注册" in n for n in res[0].notes)


def test_verify_body_md_missing_page():
    md = "公司预期净利润。[[aa.pdf#p999]]"
    res = _resolver_with(1, "aa.pdf", {"p5": "公司预期净利润 50 亿"})
    res = verify_body_md(md, resolver=res)
    assert len(res) == 1
    assert res[0].needs_review is True
    assert any("p999" in n for n in res[0].notes)


def test_verify_body_md_mismatch_quote_low_score():
    """真实数字差异（99 vs 50）必须让 verified=False，哪怕 score 偏高。"""
    md = "公司预期净利润 99 亿。[[aa.pdf#p5]]"
    res = _resolver_with(1, "aa.pdf", {"p5": "公司预期净利润 50 亿"})
    res = verify_body_md(md, resolver=res)
    assert res[0].verified is False


def test_filename_resolver_lookup_with_fullwidth_chars():
    res = FilenameResolver.build([(7, "招商证券_2026Q1策略.pdf", {"p1": "测试文本"})])
    rid = res.resolve_id("招商证券_2026Q1策略.pdf")
    assert rid == 7


def test_filename_resolver_lookup_with_basename():
    res = FilenameResolver.build([(7, "招商证券_2026Q1策略.pdf", {"p1": "测试文本"})])
    rid = res.resolve_id("data/reports/招商证券_2026Q1策略.pdf")
    assert rid == 7


# ---------------------------------------------------------------------------
# 句切分
# ---------------------------------------------------------------------------
def test_split_sentences_chinese():
    s = "第一句。第二句！第三句？"
    out = split_into_sentences(s)
    assert len(out) == 3


def test_every_sentence_has_citation_pass():
    md = (
        "## 标题\n\n"
        "公司业绩稳健。[[aa.pdf#p1]]\n\n"
        "盈利能力提升。[[aa.pdf#p2]]\n"
    )
    ok, issues = cite_svc.every_nontrivial_sentence_has_citation(md)
    assert ok
    assert issues == []


def test_every_sentence_has_citation_fail():
    md = "公司业绩稳健。盈利能力提升。[[aa.pdf#p1]]"
    ok, issues = cite_svc.every_nontrivial_sentence_has_citation(md)
    assert not ok
    assert len(issues) >= 1
