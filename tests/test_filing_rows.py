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


def test_scope_recovers_from_wrong_claimed_page():
    """LLM 把页码写偏：母公司的期初标成合并那页、合并的期初标成母公司那页，
    声称页上核不到数值时应跨页找「科目名+数值」真正所在的位置定归属。"""
    units = [
        {"n": 55, "text": "合并现金流量表\n五、现金及现金等价物净增加额 -2,049,648,127.52\n"
                          "加：期初现金及现金等价物余额 8,414,559,401.82\n"
                          "六、期末现金及现金等价物余额 6,364,911,274.30"},
        {"n": 56, "text": "母公司现金流量表\n五、现金及现金等价物净增加额 1,205,368,470.11\n"
                          "加：期初现金及现金等价物余额 4,251,726,223.05\n"
                          "六、期末现金及现金等价物余额 5,457,094,693.16"},
    ]
    rows = [
        # 页码互串：合并数值标到 56 页、母公司数值标到 55 页
        {"statement": "现金流量表", "scope": "母公司", "item": "期初现金及现金等价物余额",
         "value": 8414559401.82, "unit": "元", "page": 56},
        {"statement": "现金流量表", "scope": "合并", "item": "期初现金及现金等价物余额",
         "value": 4251726223.05, "unit": "元", "page": 55},
        {"statement": "现金流量表", "scope": "合并", "item": "期末现金及现金等价物余额",
         "value": 6364911274.30, "unit": "元", "page": 55},
    ]
    assert filing._mechanical_scopes(units, rows) == ["合并", "母公司", "合并"]


def test_scope_falls_back_to_claimed_page_when_value_nowhere():
    """数值哪页都核不到（表格区内容不在页文本里），退回声称页的表头归属。"""
    units = [{"n": 1, "text": "合并资产负债表\n货币资金 100 200"}]
    rows = [{"statement": "资产负债表", "scope": "母公司", "item": "货币资金",
             "value": 999, "unit": "元", "page": 1}]
    assert filing._mechanical_scopes(units, rows) == ["合并"]


def test_scope_uses_value_position_when_item_absent():
    """权益变动表行只有数字没有行标：期初未分配利润在页上核不到科目名时，
    按数值位置归属，不能继承上一页的母公司口径（茅台 2025 年报 R3 挂起的根因）。"""
    units = [
        {"n": 66, "text": "母公司现金流量表\n经营活动产生的现金流量净额 1 2"},
        {"n": 68, "text": "合并所有者权益变动表\n2025年度\n"
                          "一、上年年末余额 1,256,197,800.00 182,787,415,205.05 "
                          "233,105,984,399.47 8,905,330,721.13 242,011,315,120.60"},
        {"n": 70, "text": "母公司所有者权益变动表\n2025年度\n"
                          "一、上年年末余额 1,256,197,800.00 115,219,987,975.81 "
                          "164,486,282,337.92"},
    ]
    rows = [
        {"statement": "所有者权益变动表", "scope": "合并", "item": "期初未分配利润",
         "value": 182787415205.05, "unit": "元", "page": 68},
        {"statement": "所有者权益变动表", "scope": "合并", "item": "期初未分配利润",
         "value": 115219987975.81, "unit": "元", "page": 70},
    ]
    assert filing._mechanical_scopes(units, rows) == ["合并", "母公司"]


def test_scope_matches_item_with_colon_prefix():
    """科目名带「加：」前缀时也要能在页上定位：冒号隔开不算找不到，
    否则合并与母公司的期初余额会按页首继承互换归属（宇通 2025 年报 R2 挂起的根因）。"""
    units = [
        {"n": 54, "text": "母公司利润表\n一、营业收入 1 2"},
        {"n": 55, "text": "合并现金流量表\n五、现金及现金等价物净增加额 -2,049,648,127.52\n"
                          "加：期初现金及现金等价物余额 8,414,559,401.82\n"
                          "六、期末现金及现金等价物余额 6,364,911,274.30"},
        {"n": 56, "text": "母公司现金流量表\n加：期初现金及现金等价物余额 4,251,726,223.05\n"
                          "六、期末现金及现金等价物余额 1,507,359,992.46"},
    ]
    rows = [
        {"statement": "现金流量表", "scope": "合并", "item": "加：期初现金及现金等价物余额",
         "value": 8414559401.82, "unit": "元", "page": 55},
        {"statement": "现金流量表", "scope": "母公司", "item": "加：期初现金及现金等价物余额",
         "value": 4251726223.05, "unit": "元", "page": 56},
    ]
    assert filing._mechanical_scopes(units, rows) == ["合并", "母公司"]


def test_scope_marks_bare_company_header_as_parent():
    """德勤版式的母公司表头是「公司资产负债表」（不带母公司三字），
    页首独立成行按母公司算，母公司页上的行不能混进合并桶（长城 2025 年报 R3 挂起的根因）。"""
    units = [
        {"n": 142, "text": "长城汽车股份有限公司\n2025年12月31日\n合并资产负债表\n"
                          "货币资金 43,198,725,524.04 40,159,904,761.87"},
        {"n": 144, "text": "长城汽车股份有限公司\n2025年12月31日\n公司资产负债表\n"
                          "货币资金 20,313,570,603.06 19,022,657,264.28"},
        {"n": 145, "text": "长城汽车股份有限公司\n2025年12月31日\n公司资产负债表 - 续\n"
                          "未分配利润 7,872,302,158.31 6,978,313,476.85"},
    ]
    assert filing._scope_marks(units[0]["text"]) == [(23, "合并")]
    assert filing._scope_marks(units[1]["text"]) == [(23, "母公司")]
    assert filing._scope_marks(units[2]["text"]) == [(23, "母公司")]
    rows = [
        {"statement": "资产负债表", "scope": "合并", "item": "货币资金",
         "value": 43198725524.04, "unit": "元", "page": 142},
        {"statement": "资产负债表", "scope": "合并", "item": "货币资金",
         "value": 20313570603.06, "unit": "元", "page": 144},
        {"statement": "资产负债表", "scope": "合并", "item": "未分配利润",
         "value": 7872302158.31, "unit": "元", "page": 145},
    ]
    assert filing._mechanical_scopes(units, rows) == ["合并", "母公司", "母公司"]


def test_equity_movement_positive_sign_flipped_when_negative_on_page():
    """提取盈余公积在未分配利润列是减项；模型抄成盈余公积列的正数时，
    同页原行能核到负数就翻回，否则 R3 靠容差勉强通过、入库值是错符号的。"""
    page = ("合并所有者权益变动表\n"
            "1．提取盈余公积 1,706,238,132.67 -1,706,238,132.67\n")
    rows = [{"statement": "所有者权益变动表", "scope": "合并", "item": "提取盈余公积",
             "value": 1706238132.67, "unit": "元", "page": 1}]
    cons, _parent, _dropped = filing._normalize_rows(
        Path("x.pdf"), rows, None, {1: page})
    assert cons["提取盈余公积"]["v"] == -1706238132.67


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
