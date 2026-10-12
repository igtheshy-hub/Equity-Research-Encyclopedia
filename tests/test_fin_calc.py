"""fin_calc 纯函数自测：单位归一、科目归一、勾稽容差、单季差分、TTM、平均口径、派生指标。用法：python tests/test_fin_calc.py"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import fin_calc  # noqa: E402

FAILS = []


def pd(items):
    return {"items": {k: {"v": v, "a": "[[t.pdf#p1]]"} for k, v in items.items()}}


# 合成 store：营业收入等流量科目按 A 股累计披露口径造数
STORE = {
    "slug": "测试公司",
    "periods": {
        "2024Q3": pd({"营业收入": 440e9, "营业成本": 350e9, "归母净利润": 22e9}),
        "2024FY": pd({"营业收入": 600e9, "营业成本": 480e9, "净利润": 30e9, "归母净利润": 30e9,
                      "资产总计": 500e9, "所有者权益合计": 200e9, "应收账款": 50e9, "存货": 40e9}),
        "2025Q1": pd({"营业收入": 150e9, "营业成本": 120e9, "归母净利润": 8e9}),
        "2025H1": pd({"营业收入": 310e9, "营业成本": 245e9, "归母净利润": 17e9}),
        "2025Q3": pd({"营业收入": 470e9, "营业成本": 370e9, "归母净利润": 26e9}),
        "2025FY": pd({"营业收入": 640e9, "营业成本": 500e9, "净利润": 35e9, "归母净利润": 35e9,
                      "资产总计": 560e9, "负债合计": 330e9, "所有者权益合计": 230e9,
                      "应收账款": 60e9, "存货": 45e9,
                      "经营活动产生的现金流量净额": 38.5e9,
                      "购建固定资产无形资产和其他长期资产支付的现金": 15e9}),
    },
}


def check(label, got, want, tol=1e-9):
    if isinstance(want, float):
        ok = got is not None and abs(got - want) <= tol * max(1.0, abs(want))
    else:
        ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        FAILS.append(label)


def test_units():
    print("[单位与数值归一]")
    check("to_yuan 万元", fin_calc.to_yuan(123, "万元"), 123e4)
    check("to_yuan 千元", fin_calc.to_yuan(1234, "千元"), 1234e3)
    check("to_yuan 亿元", fin_calc.to_yuan(1.5, "亿元"), 1.5e8)
    check("to_yuan 人民币元", fin_calc.to_yuan(7, "人民币元"), 7.0)
    check("to_yuan 百分数 None", fin_calc.to_yuan(5.2, "%"), None)
    check("to_yuan 非金额 None", fin_calc.to_yuan(3, "件"), None)
    check("parse_value 千分位", fin_calc.parse_value("1,234"), 1234.0)
    check("parse_value 括号负数", fin_calc.parse_value("(1,234)"), -1234.0)
    check("parse_value 全角负号", fin_calc.parse_value("−78.5"), -78.5)
    check("parse_value 非数字", fin_calc.parse_value("abc"), None)
    check("parse_value None", fin_calc.parse_value(None), None)


def test_normalize():
    print("[科目归一]")
    check("别名 其中：利息费用", fin_calc.normalize_item("其中：利息费用"), ("财务费用_利息费用", True))
    check("前缀剥离 其中：营业税金及附加", fin_calc.normalize_item(" 其中： 营业税金及附加 "), ("税金及附加", True))
    check("旧名 可供出售金融资产", fin_calc.normalize_item("可供出售金融资产"), ("其他债权投资", True))
    check("字典外保留原名", fin_calc.normalize_item("星球大战基金"), ("星球大战基金", False))
    check("序号前缀 一、营业收入", fin_calc.normalize_item("一、 营业收入"), ("营业收入", True))
    check("尾部括号 营业收入（元）", fin_calc.normalize_item("营业收入（元）"), ("营业收入", True))


def test_reconciliation():
    print("[勾稽校验]")
    ok_items = {
        "资产总计": 560e9, "负债合计": 330e9, "所有者权益合计": 230e9,
        "现金及现金等价物净增加额": 20e9,
        "经营活动产生的现金流量净额": 50e9, "投资活动产生的现金流量净额": -60e9,
        "筹资活动产生的现金流量净额": 28e9, "汇率变动对现金及现金等价物的影响": 2e9,
        "期初现金及现金等价物余额": 80e9, "期末现金及现金等价物余额": 100e9,
        "未分配利润": 90e9, "期初未分配利润": 70e9, "净利润": 35e9,
        "提取盈余公积": -3.5e9, "对所有者或股东的分配": -11.5e9,  # 变动行按报表原符号（减项为负）
    }
    check("R1-R3 全平", fin_calc.check_reconciliations(ok_items, 0.005), [])
    bad_r1 = dict(ok_items, 资产总计=570e9)
    fails = fin_calc.check_reconciliations(bad_r1, 0.005)
    check("R1 不平报 1 条", len(fails), 1)
    check("R1 失败含标识", fails[0].startswith("R1") if fails else False, True)
    # 数据缺 → 跳过不算失败
    check("缺数据跳过", fin_calc.check_reconciliations({"资产总计": 100.0}, 0.005), [])


def test_periods():
    print("[报告期运算]")
    s = STORE["periods"]
    check("Q4=年报减三季报", fin_calc.single(s, "2025FY", "营业收入"), 170e9)
    check("Q1 即累计", fin_calc.single(s, "2025Q1", "营业收入"), 150e9)
    check("单季 H1−Q1", fin_calc.single(s, "2025H1", "营业收入"), 160e9)
    check("缺上期 None", fin_calc.single(s, "2024Q3", "营业收入"), None)
    check("TTM 季报", fin_calc.ttm(s, "2025Q3", "营业收入"), 630e9)  # 470+600−440
    check("TTM 年报即自身", fin_calc.ttm(s, "2025FY", "营业收入"), 640e9)
    check("TTM 缺上年同期 None", fin_calc.ttm(s, "2025H1", "营业收入"), None)
    check("平均口径年初取上年年报", fin_calc.avg_balance(s, "2025FY", "所有者权益合计"), 215e9)
    check("同比", fin_calc.yoy(s, "2025FY", "营业收入"), 640 / 600 - 1)
    check("单季同比", fin_calc.single_yoy(s, "2025FY", "归母净利润"), (35 - 26) / (30 - 22) - 1)


def test_indicators():
    print("[派生指标]")
    s = STORE["periods"]
    ind, anc, missing = fin_calc.compute_indicators(s, "2025FY")
    check("毛利率(累计)", ind.get("毛利率(累计)"), (640 - 500) / 640)
    check("毛利率(单季)", ind.get("毛利率(单季)"), (170 - 130) / 170)
    check("ROE(平均) 年初取上年年报", ind.get("ROE(平均)"), 35 / 215)
    check("ROE(归母平均) 缺归母权益回退", ind.get("ROE(归母平均)"), 35 / 215)
    check("DIO(期末) 用营业成本", ind.get("DIO(期末)"), 45e9 / 500e9 * 365)
    check("资产负债率", ind.get("资产负债率"), 330 / 560)
    check("获现比(TTM)", ind.get("获现比(TTM)"), 38.5 / 35)
    check("FCF(标准)", ind.get("FCF(标准)"), 38.5e9 - 15e9)
    check("营收同比", ind.get("营收同比"), 640 / 600 - 1)
    check("锚点已带且去重", anc.get("毛利率(累计)"), ["[[t.pdf#p1]]"])
    check("缺利息费用进缺失清单", "FCF(严格)" in missing, True)
    check("缺上年平均余额口径不炸", isinstance(missing, list), True)


if __name__ == "__main__":
    test_units()
    test_normalize()
    test_reconciliation()
    test_periods()
    test_indicators()
    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {'、'.join(FAILS)}")
        sys.exit(1)
    print("ALL PASS")
