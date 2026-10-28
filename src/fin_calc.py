"""财报计算器：单位归一、科目归一、勾稽校验、报告期运算与派生指标。

口径依据 schema/财报摄取-03-多口径指标字典.md；科目依据 schema/filing_items.json。
所有计算纯机械，不做任何判断。R4（净利润 vs 补充资料起点）因补充资料行名歧义暂不实现，
勾稽由 R1—R3 与 R2 的两条子校验覆盖。
"""
import json
import re

from . import config

_ITEMS = None
_ALIAS = None

UNIT_FACTORS = {
    "元": 1, "人民币元": 1, "人民币": 1,
    "百元": 100, "千元": 1e3, "千": 1e3,
    "万": 1e4, "万元": 1e4,
    "十万": 1e5, "百万": 1e6, "百万元": 1e6,
    "千万": 1e7, "亿": 1e8, "亿元": 1e8,
}

PERIOD_RE = re.compile(r"(\d{4})(FY|H1|Q1|Q3)")
# 报告期推进顺序：Q1 → H1 → Q3 → FY
_PREV_TYPE = {"H1": "Q1", "Q3": "H1", "FY": "Q3"}


def _clean_name(s):
    s = str(s).strip().replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", s)


_PREFIX_RE = re.compile(r"^(?:其中：|加：|减：|[一二三四五六七八九十]+、|\d+、|\d+[．.])")


def load_items():
    global _ITEMS, _ALIAS
    if _ITEMS is None:
        data = json.loads((config.SCHEMA_DIR / "filing_items.json").read_text(encoding="utf-8"))
        _ITEMS = data["items"]
        _ALIAS = {}
        for name, spec in _ITEMS.items():
            _ALIAS.setdefault(_clean_name(name), name)
            for a in spec.get("aliases", []):
                _ALIAS.setdefault(_clean_name(a), name)
    return _ITEMS


def normalize_item(name):
    """科目名归一到字典规范名。返回 (规范名, 是否字典内)。
    逐级尝试：原名 → 剥序号/其中前缀 → 剥尾部括号段（如「营业收入（元）」「实收资本（或股本）」）。"""
    items = load_items()
    key = _clean_name(name)
    if key in _ALIAS:
        return _ALIAS[key], True
    key2 = _PREFIX_RE.sub("", key, count=1)
    if key2 != key and key2 in _ALIAS:
        return _ALIAS[key2], True
    key3 = re.sub(r"\([^()]*\)$", "", key2)
    if key3 != key2 and key3 in _ALIAS:
        return _ALIAS[key3], True
    return str(name).strip(), False


def parse_value(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg or s.startswith("（") and s.endswith("）"):
        s = s.strip("()（）")
    s = s.replace("−", "-").replace("--", "-")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


def normalize_unit(unit):
    return str(unit or "").strip().replace("％", "%").replace("／", "/")


def is_percent_unit(unit):
    return "%" in normalize_unit(unit)


def is_per_share_unit(unit):
    u = normalize_unit(unit)
    return "/股" in u or "每股" in u


def item_entry(store_or_pd, pk, name):
    """取某一期的科目原始记录（含 v / a / unit）。store_or_pd 可以是整份 store 或单期。"""
    if store_or_pd and "periods" in store_or_pd:
        pd_ = store_or_pd.get("periods", {}).get(pk) or {}
    else:
        pd_ = store_or_pd or {}
    return (pd_.get("items") or {}).get(name) or {}


def item_unit(store_or_pd, pk, name):
    return normalize_unit(item_entry(store_or_pd, pk, name).get("unit"))


def to_yuan(value, unit):
    """按披露单位换算成元；百分数与非金额单位返回 None（宁可丢数不可错数）。"""
    if value is None:
        return None
    u = str(unit or "元").strip().replace("％", "%")
    if "%" in u:
        return None
    u = re.sub(r"[()（）]|人民币|/股|每股", "", u).strip() or "元"
    factor = UNIT_FACTORS.get(u)
    if factor is None:
        if "亿" in u:
            factor = 1e8
        elif "万" in u:
            factor = 1e4
        elif "元" in u:
            factor = 1
        else:
            return None
    return float(value) * factor


def period_key(ptype, year):
    return f"{year}{ptype}"


def parse_period_key(pk):
    m = PERIOD_RE.fullmatch(str(pk))
    if not m:
        raise ValueError(f"非法报告期键: {pk}")
    return int(m.group(1)), m.group(2)


def prior_same_key(pk):
    y, t = parse_period_key(pk)
    return period_key(t, y - 1)


def prior_fy_key(pk):
    y, _ = parse_period_key(pk)
    return period_key("FY", y - 1)


def _near(a, b, tol):
    ref = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= tol * ref


def check_reconciliations(items, tol):
    """勾稽校验。items: {规范名: 元值}。返回失败清单（空=通过）；数据缺的规则跳过。"""
    fails = []

    def g(*names):
        for n in names:
            if items.get(n) is not None:
                return items[n]
        return None

    a, l, e = g("资产总计"), g("负债合计"), g("所有者权益合计")
    if None not in (a, l, e) and not _near(a, l + e, tol):
        fails.append(f"R1 资产≠负债+权益：{a:,.0f} vs {l + e:,.0f}")

    net = g("现金及现金等价物净增加额")
    ocf = g("经营活动产生的现金流量净额")
    icf = g("投资活动产生的现金流量净额")
    fcf_ = g("筹资活动产生的现金流量净额")
    fx = g("汇率变动对现金及现金等价物的影响") or 0.0
    if None not in (net, ocf, icf, fcf_) and not _near(net, ocf + icf + fcf_ + fx, tol):
        fails.append(f"R2 净增加额 {net:,.0f} ≠ 三大活动净额+汇率 {ocf + icf + fcf_ + fx:,.0f}")
    beg, end = g("期初现金及现金等价物余额"), g("期末现金及现金等价物余额")
    if None not in (net, beg, end) and not _near(end - beg, net, tol):
        fails.append(f"R2 期末-期初 {end - beg:,.0f} ≠ 净增加额 {net:,.0f}")

    # R3 容差放宽 5 倍：权益结转、OCI 结转留存收益等小项不逐项核对。
    # 变动行（提取盈余公积/对股东的分配）按报表原符号（括号负数抄负号）代入代数和
    pe, pb = g("未分配利润"), g("期初未分配利润")
    ni = g("归母净利润", "净利润")
    appr, div = g("提取盈余公积"), g("对所有者或股东的分配")
    if None not in (pe, pb, ni, appr, div):
        target = pb + ni + appr + div
        if not _near(pe, target, tol * 5):
            fails.append(f"R3 未分配利润勾稽不平：期末 {pe:,.0f} vs 期初+净利+提取+分配(代数和) {target:,.0f}")
    return fails


# --- 报告期运算（store = fin json 的 periods dict；pd_ = 单期数据） ---

def _v(pd_, name):
    return (pd_ or {}).get("items", {}).get(name, {}).get("v")


def cum(store, pk, name):
    return _v(store.get(pk), name)


def anchors_of_item(store, pk, name):
    a = (store.get(pk) or {}).get("items", {}).get(name, {}).get("a")
    return [a] if a else []


def ttm(store, pk, name):
    """滚动十二个月 = 本期累计 + 上年年报 − 上年同期累计；年报即自身。"""
    cur = cum(store, pk, name)
    if cur is None:
        return None
    y, t = parse_period_key(pk)
    if t == "FY":
        return cur
    fy, same = cum(store, prior_fy_key(pk), name), cum(store, prior_same_key(pk), name)
    if fy is None or same is None:
        return None
    return cur + fy - same


def single(store, pk, name):
    """单季值：Q1 即累计；H1/Q3/FY 与上一报告期累计差分（FY 得 Q4）。"""
    y, t = parse_period_key(pk)
    cur = cum(store, pk, name)
    if cur is None or t == "Q1":
        return cur
    prev = cum(store, period_key(_PREV_TYPE[t], y), name)
    if prev is None:
        return None
    return cur - prev


def yoy(store, pk, name):
    cur, same = cum(store, pk, name), cum(store, prior_same_key(pk), name)
    if cur is None or same in (None, 0):
        return None
    return cur / same - 1


def single_yoy(store, pk, name):
    cur, same = single(store, pk, name), single(store, prior_same_key(pk), name)
    if cur is None or same in (None, 0):
        return None
    return cur / same - 1


def avg_balance(store, pk, name):
    """平均口径 =（年初 + 期末）/2，年初取上年年报期末。"""
    y, _ = parse_period_key(pk)
    cur, beg = cum(store, pk, name), cum(store, period_key("FY", y - 1), name)
    if cur is None or beg is None:
        return None
    return (cur + beg) / 2


def compute_indicators(store, pk):
    """按指标字典计算当期全部派生指标。返回 (indicators, anchors)：
    indicators {指标名(口径): 数值}，anchors {指标名(口径): [锚点]}（取数科目锚点集合）。"""
    ind, anc, missing = {}, {}, []
    pd_ = store.get(pk) or {}

    def put(name, value, *src_names):
        if value is None:
            missing.append(name)
            return
        ind[name] = value
        seen = []
        for sn in src_names:
            for a in anchors_of_item(store, pk, sn):
                if a and a not in seen:
                    seen.append(a)
        anc[name] = seen

    rev, cogs = _v(pd_, "营业收入"), _v(pd_, "营业成本")
    gp = (rev - cogs) if None not in (rev, cogs) else None
    put("毛利率(累计)", gp / rev if gp is not None and rev else None, "营业收入", "营业成本")
    put("毛利率(单季)", (
        (single(store, pk, "营业收入") - single(store, pk, "营业成本")) / single(store, pk, "营业收入")
        if single(store, pk, "营业收入") not in (None, 0) and single(store, pk, "营业成本") is not None else None),
        "营业收入", "营业成本")
    put("毛利率(TTM)", (
        (ttm(store, pk, "营业收入") - ttm(store, pk, "营业成本")) / ttm(store, pk, "营业收入")
        if ttm(store, pk, "营业收入") not in (None, 0) and ttm(store, pk, "营业成本") is not None else None),
        "营业收入", "营业成本")

    def core_op():
        names = ["营业收入", "营业成本", "税金及附加", "销售费用", "管理费用", "研发费用", "财务费用"]
        vals = [_v(pd_, n) for n in names]
        if any(v is None for v in vals) or not vals[0]:
            return None
        return vals[0] - sum(vals[1:])

    put("核心营业利润率(自定义)", core_op() / rev if core_op() is not None and rev else None,
        "营业收入", "营业成本", "税金及附加", "销售费用", "管理费用", "研发费用", "财务费用")
    tot_rev = _v(pd_, "营业总收入") or rev
    op, ni = _v(pd_, "营业利润"), _v(pd_, "净利润")
    put("营业利润率", op / tot_rev if op is not None and tot_rev else None, "营业利润", "营业总收入", "营业收入")
    put("净利率", ni / tot_rev if ni is not None and tot_rev else None, "净利润", "营业总收入", "营业收入")
    pn = _v(pd_, "归母净利润")
    put("归母净利率", pn / tot_rev if pn is not None and tot_rev else None, "归母净利润", "营业总收入", "营业收入")

    wroe = _v(pd_, "加权平均净资产收益率")
    # 只看披露单位：单位为 % 时 15.31 表示 15.31%，除以 100 存成 0.1531。
    # 不再用 abs>1.5 猜测——那会把以小数披露（0.1531）的 ROE 误缩小、把高比例误放大。
    if wroe is not None and is_percent_unit(item_unit(pd_, "加权平均净资产收益率")):
        wroe = wroe / 100
    put("ROE(加权披露)", wroe, "加权平均净资产收益率")
    eq, eq_p = _v(pd_, "所有者权益合计"), _v(pd_, "归属于母公司所有者权益合计") or _v(pd_, "所有者权益合计")
    put("ROE(期末)", ni / eq if ni is not None and eq else None, "净利润", "所有者权益合计")
    eq_avg = avg_balance(store, pk, "所有者权益合计")
    put("ROE(平均)", ni / eq_avg if ni is not None and eq_avg else None, "净利润", "所有者权益合计")
    eq_p_avg = avg_balance(store, pk, "归属于母公司所有者权益合计") or (
        (avg_balance(store, pk, "所有者权益合计") + avg_balance(store, pk, "所有者权益合计")) / 2
        if avg_balance(store, pk, "所有者权益合计") is not None else None)
    if eq_p_avg is None:
        eq_p_avg = eq_avg
    put("ROE(归母平均)", pn / eq_p_avg if pn is not None and eq_p_avg else None,
        "归母净利润", "归属于母公司所有者权益合计")
    ni_ttm = ttm(store, pk, "净利润")
    pn_ttm = ttm(store, pk, "归母净利润")
    put("ROE(TTM)", (pn_ttm if pn_ttm is not None else ni_ttm) / eq_p_avg
        if (pn_ttm or ni_ttm) is not None and eq_p_avg else None, "净利润", "归母净利润")
    a_avg = avg_balance(store, pk, "资产总计")
    put("ROA(平均)", ni / a_avg if ni is not None and a_avg else None, "净利润", "资产总计")

    def roic():
        ptp, tax, ie = _v(pd_, "利润总额"), _v(pd_, "所得税费用"), _v(pd_, "财务费用_利息费用")
        if None in (ptp, tax) or not ptp:
            return None, False
        rate = tax / ptp
        debt_names = ["短期借款", "一年内到期的非流动负债", "长期借款", "应付债券", "租赁负债"]
        invested = eq
        if invested is None:
            return None, False
        for n in debt_names:
            x = _v(pd_, n)
            if x is not None:
                invested += x
        if ie is not None:
            return (ptp + ie) * (1 - rate) / invested, False
        return (ptp + (_v(pd_, "财务费用") or 0)) * (1 - rate) / invested, True  # 利息缺失以财务费用近似

    v_roic, degraded = roic()
    put("ROIC" + ("(degraded)" if degraded else ""), v_roic,
        "利润总额", "所得税费用", "所有者权益合计", "短期借款", "长期借款", "应付债券", "租赁负债")

    tl, ta = _v(pd_, "负债合计"), _v(pd_, "资产总计")
    put("资产负债率", tl / ta if None not in (tl, ta) and ta else None, "负债合计", "资产总计")
    ncl = _v(pd_, "非流动负债合计")
    put("长期负债率", ncl / ta if None not in (ncl, ta) and ta else None, "非流动负债合计", "资产总计")
    put("权益乘数", ta / eq if None not in (ta, eq) and eq else None, "资产总计", "所有者权益合计")

    ca, cl, inv = _v(pd_, "流动资产合计"), _v(pd_, "流动负债合计"), _v(pd_, "存货")
    put("流动比率", ca / cl if None not in (ca, cl) and cl else None, "流动资产合计", "流动负债合计")
    put("速动比率", (ca - inv) / cl if None not in (ca, cl, inv) and cl else None,
        "流动资产合计", "存货", "流动负债合计")
    qr = ["货币资金", "交易性金融资产", "应收票据", "应收账款"]
    qr_sum = sum(_v(pd_, n) or 0 for n in qr)
    put("保守速动比率", qr_sum / cl if cl and all(_v(pd_, n) is not None for n in qr) else None, *qr, "流动负债合计")

    def days(balance, flow_name):
        if balance is None:
            return None
        flow = ttm(store, pk, flow_name)
        if not flow:
            return None
        return balance / flow * 365

    ar = _v(pd_, "应收账款")
    ar_avg = avg_balance(store, pk, "应收账款")
    ar_bills = sum(_v(pd_, n) or 0 for n in ("应收票据", "应收账款", "应收款项融资"))
    ar_bills_avg = ar_avg + (avg_balance(store, pk, "应收票据") or 0) + (avg_balance(store, pk, "应收款项融资") or 0) \
        if ar_avg is not None else None
    put("DSO(期末)", days(ar, "营业收入"), "应收账款", "营业收入")
    put("DSO(平均)", days(ar_avg, "营业收入"), "应收账款", "营业收入")
    put("DSO(平均含票据)", days(ar_bills_avg, "营业收入")
        if ar_bills_avg is not None and None not in (_v(pd_, "应收票据"), _v(pd_, "应收款项融资")) else None,
        "应收票据", "应收账款", "应收款项融资")
    inv_avg = avg_balance(store, pk, "存货")
    put("DIO(期末)", days(inv, "营业成本"), "存货", "营业成本")
    put("DIO(平均)", days(inv_avg, "营业成本"), "存货", "营业成本")
    ap = _v(pd_, "应付账款")
    ap_avg = avg_balance(store, pk, "应付账款")
    ap_bills_avg = ap_avg + (avg_balance(store, pk, "应付票据") or 0) if ap_avg is not None else None
    put("DPO(期末)", days(ap, "营业成本"), "应付账款", "营业成本")
    put("DPO(平均)", days(ap_avg, "营业成本"), "应付账款", "营业成本")
    put("DPO(平均含票据)", days(ap_bills_avg, "营业成本")
        if ap_bills_avg is not None and _v(pd_, "应付票据") is not None else None, "应付票据", "应付账款")
    for label, dso_name, dio_name, dpo_name in (
        ("CCC(期末)", "DSO(期末)", "DIO(期末)", "DPO(期末)"),
        ("CCC(平均)", "DSO(平均)", "DIO(平均)", "DPO(平均)"),
        ("CCC(平均含票据)", "DSO(平均含票据)", "DIO(平均)", "DPO(平均含票据)"),
    ):
        dso, dio, dpo = ind.get(dso_name), ind.get(dio_name), ind.get(dpo_name)
        if None not in (dso, dio, dpo):
            ind[label] = dso + dio - dpo
            # CCC 是三个分项算出来的，锚点取三项来源科目的并集，不能是空列表
            seen = []
            for part in (dso_name, dio_name, dpo_name):
                for a in anc.get(part) or []:
                    if a not in seen:
                        seen.append(a)
            anc[label] = seen
        else:
            missing.append(label)
    put("总资产周转率", ttm(store, pk, "营业收入") / a_avg
        if ttm(store, pk, "营业收入") is not None and a_avg else None, "营业收入", "资产总计")

    ocf_v = _v(pd_, "经营活动产生的现金流量净额")
    ni_ttm_v = ni_ttm if ni_ttm is not None else ttm(store, pk, "净利润")
    put("获现比(TTM)", ocf_v / ni_ttm_v if ocf_v is not None and ni_ttm_v else None,
        "经营活动产生的现金流量净额", "净利润")
    capex = _v(pd_, "购建固定资产无形资产和其他长期资产支付的现金")
    put("资本支出(元)", capex, "购建固定资产无形资产和其他长期资产支付的现金")
    put("FCF(标准)", ocf_v - capex if None not in (ocf_v, capex) else None,
        "经营活动产生的现金流量净额", "购建固定资产无形资产和其他长期资产支付的现金")
    ie = _v(pd_, "财务费用_利息费用")
    put("FCF(严格)", ocf_v - capex - ie if None not in (ocf_v, capex, ie) else None,
        "经营活动产生的现金流量净额", "购建固定资产无形资产和其他长期资产支付的现金", "财务费用_利息费用")

    def equity_surplus():
        dep_names = ["固定资产折旧", "无形资产摊销", "长期待摊费用摊销"]
        deps = [_v(pd_, n) for n in dep_names]
        if ni is None or capex is None or any(d is None for d in deps):
            return None
        return ni + sum(deps) - capex

    put("股东盈余(元)", equity_surplus(), "净利润", "固定资产折旧", "无形资产摊销", "长期待摊费用摊销",
        "购建固定资产无形资产和其他长期资产支付的现金")

    put("营收同比", yoy(store, pk, "营业收入"), "营业收入")
    put("营收单季同比", single_yoy(store, pk, "营业收入"), "营业收入")
    put("归母净利同比", yoy(store, pk, "归母净利润"), "归母净利润")
    put("归母净利单季同比", single_yoy(store, pk, "归母净利润"), "归母净利润")

    shares = _v(pd_, "实收资本")
    pn_sq = single(store, pk, "归母净利润")
    put("EPS-单季(近似)", pn_sq / shares if pn_sq is not None and shares else None, "归母净利润", "实收资本")

    return ind, anc, missing


def indicator_unit(name):
    """派生指标的单位。指标内部一律存小数（0.1531 = 15.31%），展示时再乘 100。"""
    n = str(name)
    if "周转率" in n:
        return "次"
    if any(k in n for k in ("DSO", "DIO", "DPO")) or n.startswith("CCC"):
        return "天"
    if "EPS" in n:
        return "元/股"
    if "FCF" in n or "资本支出" in n or "股东盈余" in n:
        return "元"
    if any(k in n for k in ("率", "同比", "ROE", "ROA", "ROIC")):
        return "%"
    if any(k in n for k in ("比率", "乘数", "获现比")):
        return "倍"
    return ""


def format_item_value(value, unit):
    """【数据】行的展示格式：金额到元、百分比保留两位、每股收益保留四位，不四舍五入成整数。"""
    u = normalize_unit(unit)
    if is_percent_unit(u):
        return f"{value:.2f}%"
    if is_per_share_unit(u):
        return f"{value:.4f} 元/股"
    if u in ("", "元", "人民币元"):
        return f"{value:,.0f} 元"
    if is_percent_unit(unit) or u == "%":
        return f"{value:.2f}%"
    return f"{value:,.4f} {u}".rstrip()


def format_indicator_value(name, value):
    """【指标】行的展示格式：按指标自身单位显示，避免把 ROE 写成「15 元」、把 EPS 抹成整数。"""
    u = indicator_unit(name)
    if u == "%":
        return f"{value * 100:.2f}%"
    if u == "天":
        return f"{value:.2f} 天"
    if u == "倍":
        return f"{value:.2f} 倍"
    if u == "次":
        return f"{value:.2f} 次"
    if u == "元/股":
        return f"{value:.4f} 元/股"
    if u == "元":
        return f"{value:,.0f} 元"
    return f"{value:.4f}"
