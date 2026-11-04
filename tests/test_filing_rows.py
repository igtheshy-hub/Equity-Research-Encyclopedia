"""财报抄录的两个精度坑：序号行首不挡下一行数值，三位小数不被格式化成两位。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import filing  # noqa: E402


def test_number_on_next_line_after_enumerated_label():
    text = ("1.归属于母公司股东的净利润（净亏损以“-”\n"
            "5,637,819,345.35 8,100,867,339.79\n"
            "号填列）")
    item = "1.归属于母公司股东的净利润（净亏损以“-”号填列）"
    assert filing._value_on_page(text, item, 5637819345.35, "元") is True


def test_three_decimal_eps_keeps_precision():
    assert filing._fmt_value(0.493) == "0.493"
    assert filing._fmt_value(0.042) == "0.042"
    assert filing._fmt_value(123456789.0) == "123,456,789"
    # 千元→元换算的浮点尾数不能写进知识行
    assert filing._fmt_value(950583511046.530029) == "950,583,511,046.53"


def test_json_thousand_separator_repaired():
    from src import llm
    out = llm._extract_json('{"rows": [{"value": 1,234,567.89, "unit": "元"}]}')
    assert out["rows"][0]["value"] == 1234567.89


def test_missing_statement_header_page_is_added():
    units = [{"n": 6, "text": "四、季度财务报表\n（二）财务报表\n合并资产负债表\n2026年9月30日\n货币资金 100 90"}]
    meta = {"sections": {"key": [2], "bs": [7], "is": []}}
    added = filing._ensure_header_pages(units, meta)
    assert added == [(6, "bs")]
    assert meta["sections"]["bs"] == [6, 7]
    # 已在定位集合里的页不再重复补
    assert filing._ensure_header_pages(units, meta) == []


def test_scope_switches_at_parent_header_midpage():
    units = [
        {"n": 1, "text": "合并资产负债表\n货币资金 100 200\n负债合计 50 60\n"
                          "母公司资产负债表\n货币资金 30 40\n负债合计 10 20"},
    ]
    rows = [
        {"statement": "资产负债表", "scope": "合并", "item": "货币资金", "value": 100, "unit": "元", "page": 1},
        {"statement": "资产负债表", "scope": "合并", "item": "货币资金", "value": 30, "unit": "元", "page": 1},
        {"statement": "资产负债表", "scope": "合并", "item": "负债合计", "value": 10, "unit": "元", "page": 1},
    ]
    assert filing._mechanical_scopes(units, rows) == ["合并", "母公司", "母公司"]


def test_scope_inherits_header_from_previous_page():
    units = [
        {"n": 1, "text": "母公司现金流量表\n经营活动产生的现金流量净额 1 2"},
        {"n": 2, "text": "投资活动产生的现金流量净额 3 4"},
    ]
    rows = [{"statement": "现金流量表", "scope": "合并", "item": "投资活动产生的现金流量净额",
             "value": 3, "unit": "元", "page": 2}]
    assert filing._mechanical_scopes(units, rows) == ["母公司"]


def test_primary_statement_beats_key_financial_data_for_same_item():
    # 三季报主要会计数据先出现（本报告期 0.042），利润表后出现（年初至报告期末 0.493），应保留累计口径
    rows = [
        {"statement": "主要会计数据", "scope": "合并", "item": "基本每股收益",
         "value": 0.042, "unit": "元/股", "page": 2},
        {"statement": "利润表", "scope": "合并", "item": "基本每股收益",
         "value": 0.493, "unit": "元/股", "page": 9},
    ]
    cons, _parent, _dropped = filing._normalize_rows(Path("x.pdf"), rows, None, None)
    assert cons["基本每股收益"]["v"] == 0.493
    assert cons["基本每股收益"]["statement"] == "利润表"
