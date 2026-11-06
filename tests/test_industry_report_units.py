"""表题注单位解析：括号里先出现短假单位时不能吃掉真正的单位。"""
from src import industry_report as ir


def test_unit_in_text_prefers_longest_hit():
    assert ir._unit_in_text("**表3 卖方盈利预测对照（归母净利润，人民币亿元）**") == "亿元"
    assert ir._unit_in_text("（2026E—2028E，人民币亿元）") == "亿元"
    assert ir._unit_in_text("（万人）") == "万人"
    assert ir._unit_in_text("（%）") == "%"
    assert ir._unit_in_text("（亿元，%）") == "亿元"
    assert ir._unit_in_text("归母净利润（亿元）") == "亿元"


def test_table_caption_unit_covers_series_numbers(monkeypatch):
    """题注「人民币亿元」曾解析成「人」，整表数字被补错单位后核不上原文。"""
    fname = "fake_研报.pdf"
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {fname: "fake/path.pdf"})
    monkeypatch.setattr(ir, "_anchor_extra_text", lambda pages=None: {})
    monkeypatch.setattr(ir, "load_cached", lambda path: {
        "units": [{"n": 6, "text": "我们维持26-28年归母净利润60.4/76.7/95.8亿元"}]})
    md = (
        "**表3 卖方盈利预测对照（归母净利润，人民币亿元）**\n\n"
        "| 机构 | 报告期 | 2025E | 2026E | 2027E |\n"
        "|---|---|---|---|---|\n"
        f"| 国信证券 | 2026-09-14 | 60.4 [[{fname}#para6]] | "
        f"76.7 [[{fname}#para6]] | 95.8 [[{fname}#para6]] |\n"
    )
    assert ir._table_checks(md) == []


def test_table_source_line_after_note(monkeypatch):
    """表下先有「注：」再写「数据来源：」时，来源行锚点仍要覆盖整表数字。"""
    fname = "fake_年报.pdf"
    monkeypatch.setattr(ir.wiki_check, "file_paths", lambda: {fname: "fake/path.pdf"})
    monkeypatch.setattr(ir, "_anchor_extra_text", lambda pages=None: {})
    monkeypatch.setattr(ir, "load_cached", lambda path: {
        "units": [{"n": 3, "text": "2025年营业收入414.26亿元，归母净利润55.54亿元"}]})
    md = (
        "**表1 关键财务数据（单位：亿元）**\n\n"
        "| 项目 | 2025A |\n"
        "|---|---|\n"
        "| 营业收入 | 414.26 |\n"
        "| 归母净利润 | 55.54 |\n\n"
        "注：表内数据为年报口径。\n\n"
        f"数据来源：公司年报[[{fname}#p3]]。\n"
    )
    assert ir._table_checks(md) == []
