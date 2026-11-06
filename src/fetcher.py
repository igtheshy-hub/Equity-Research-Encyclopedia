"""网页端免登录取数：每个底层站点一条专用入口，参数按站点真实可下载的数据结构设计。

口径：
- 只碰免登录源。慧博只借免登录榜单定新财富前 3 券商，不登录、不下载正文（2026-10-29 实测登录后
  详情页仍无 PDF 直链，登录功能已移除）；发现报告积分等登录墙一律 raise FetchError，提示手动上传
  或到会话派资料员，不自动填凭据、不碰验证码。
- 研报、公告与年报下载进 raw/inbox，取名与摄入复用 pipeline 的取名/摄入双线（借同名互斥锁）。
- 年报按公司名或代码取最近一期：A 股走巨潮 category_ndbg_szsh，港股走 HKEXnews 的 prefix.do +
  titleSearchServlet.do（t1code=40000），下载后自动入库。
- 结构化数字落成快照 DOCX（raw/reports/数据快照/），再直接摄入，锚点为块号。
- 2026-10-29 逐站实测：东财 report/list 的 qType 0=个股、1=行业、2=策略；巨潮公告分类代码取自
  new-notice.js；港交所分类取自 tierone_c.json / tiertwo_c.json；统计局搜索支持「地区 + 指标」
  检索；乘联会 data.cpcadata.com/api/chartlist 按 charttype 1-6 返回不同表（1/3 含两年同月对比与
  同比，2 含累计/当月两套榜，4/5/6 含销量与份额四列）；中证指数 csindex-home 下按指数代码取
  基础信息、估值、前十大权重与行业权重；人行统计页按月挂 xlsx（单位行可能在表头上方，月份可能
  跨两列）；中汽协产销/数据栏目正文全是图片，取不到数字，只保留有正文的行业要闻与协会动态；
  IMF DataMapper 的国家路径参数不生效，取回后要按请求的国家代码过滤。
"""
import csv
import html
import inspect
import io
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx

from . import config, ingest as ingest_mod, llm, snapshot, tasks
from .classify import find_duplicate, sanitize, sha256_of, unique_target
from .pipeline import build_lines

HIBOR_AUTHOR_URL = "https://www.hibor.com.cn/author.html"
HIBOR_BIGTYPE = {"1": "证券业金牛分析师", "2": "新财富最佳分析师", "3": "证券业水晶球分析师"}
EASTMONEY_LIST_URL = "https://reportapi.eastmoney.com/report/list"
EASTMONEY_PDF_URL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
NBS_BASE = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STOCK_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_PDF_BASE = "http://static.cninfo.com.cn/"
HKEX_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEX_PDF_BASE = "https://www1.hkexnews.hk"
CPCA_API_URL = "http://data.cpcadata.com/api/chartlist"
CSINDEX_BASE = "https://www.csindex.com.cn/csindex-home"
PBC_STATS_BASE = "http://www.pbc.gov.cn/diaochatongjisi/116219/116319"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
IMF_API_BASE = "https://www.imf.org/external/datamapper/api/v1"
DBNOMICS_API_BASE = "https://api.db.nomics.world/v22/series"
WORLDBANK_API_BASE = "https://api.worldbank.org/v2"
CAAM_BASE = "http://www.caam.org.cn"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# 常见别名：东财行业名与慧博榜单组名都用较宽的行业口径，窄主题先扩一层再匹配
INDUSTRY_ALIASES = {
    "乘用车": "汽车", "商用车": "汽车", "整车": "汽车", "新能源车": "汽车", "新能源汽车": "汽车",
    "白酒": "食品饮料", "啤酒": "食品饮料", "乳制品": "食品饮料",
    "锂电池": "电力设备", "光伏": "电力设备", "储能": "电力设备",
    "半导体": "电子", "芯片": "电子",
    "创新药": "医药", "医疗器械": "医药",
}

AKSHARE_SERIES = {
    # 2026-11-05 移除 4 个报告卡序列：macro_china_gdp_yearly / macro_china_cpi_yearly /
    # macro_china_cx_pmi_yearly / macro_china_cx_services_pmi_yearly 返回的是单期报告卡
    # （公布值/预测值/影响），不是时间序列，选了取不到历史值
    "macro_china_gdp": "GDP 累计值（季度）",
    "macro_china_industrial_production_yoy": "规模以上工业增加值同比",
    "macro_china_pmi": "制造业 PMI（月度）",
    "macro_china_pmi_yearly": "制造业 PMI 同比",
    "macro_china_non_man_pmi": "非制造业 PMI",
    "macro_china_enterprise_boom_index": "企业景气指数",
    "macro_china_qyspjg": "企业商品价格指数",
    "macro_china_cpi": "CPI（月度）",
    "macro_china_cpi_monthly": "CPI 环比",
    "macro_china_ppi": "PPI（月度）",
    "macro_china_ppi_yearly": "PPI 同比",
    "macro_china_retail_price_index": "零售价格指数",
    "macro_china_commodity_price_index": "大宗商品价格指数",
    "macro_china_agricultural_index": "农产品批发价格指数",
    "macro_china_agricultural_product": "农产品批发市场价格",
    "macro_china_vegetable_basket": "菜篮子产品批发价格指数",
    "macro_china_new_house_price": "新建商品住宅价格（北京/上海）",
    "macro_china_exports_yoy": "出口同比",
    "macro_china_imports_yoy": "进口同比",
    "macro_china_trade_balance": "贸易差额",
    "macro_china_hgjck": "海关进出口",
    "macro_china_wbck": "外贸出口",
    "macro_china_whxd": "外汇贷款",
    "macro_china_fdi": "外商直接投资",
    "macro_china_fx_reserves_yearly": "外汇储备",
    "macro_china_foreign_exchange_gold": "外汇与黄金储备",
    "macro_china_fx_gold": "黄金储备",
    # macro_china_rmb 已移除：akshare 1.19.3 该接口数据停在 2021-05，取到也只会是旧数，
    # 汇率改用 fred_series 的 DEXCHUS（美元兑人民币）
    "macro_china_money_supply": "货币供应量",
    "macro_china_supply_of_money": "货币供应量（分口径）",
    "macro_china_m2_yearly": "M2 同比",
    "macro_china_lpr": "LPR（月度）",
    "macro_china_shibor_all": "Shibor 全期限",
    "macro_china_bank_financing": "银行融资",
    "macro_china_shrzgm": "社会融资规模增量",
    "macro_china_new_financial_credit": "新增人民币贷款",
    "macro_china_central_bank_balance": "央行资产负债表",
    "macro_china_reserve_requirement_ratio": "存款准备金率",
    "macro_china_czsr": "财政收入",
    "macro_china_national_tax_receipts": "税收收入",
    "macro_china_consumer_goods_retail": "社会消费品零售总额",
    "macro_china_gdzctz": "固定资产投资",
    "macro_china_real_estate": "房地产开发投资",
    "macro_china_insurance": "保险业经营数据",
    "macro_china_insurance_income": "保险业保费收入",
    "macro_china_society_electricity": "全社会用电量",
    "macro_china_society_traffic_volume": "全社会客运量",
    "macro_china_stock_market_cap": "股票市场市值",
    "macro_china_mobile_number": "移动电话用户数",
    "macro_china_postal_telecommunicational": "邮电通信业务",
    "macro_china_bdti_index": "BDTI 原油运输指数",
    "macro_china_bsi_index": "BSI 干散货指数",
    "macro_china_energy_index": "能源价格指数",
    "macro_china_freight_index": "运价指数",
    "macro_china_lpi_index": "物流景气指数",
    "macro_china_construction_index": "建材价格指数",
    "macro_china_construction_price_index": "建材价格指数（分品种）",
    "macro_china_yw_electronic_index": "义乌小商品电子指数",
    "macro_china_bond_public": "债券发行",
    "macro_china_market_margin_sh": "上交所融资融券",
    "macro_china_market_margin_sz": "深交所融资融券",
    "macro_china_daily_energy": "每日能源数据",
    "macro_china_passenger_load_factor": "航空客座率",
    "macro_china_international_tourism_fx": "国际旅游外汇收入",
    "macro_china_urban_unemployment": "城镇调查失业率",
    "macro_china_hk_cpi": "香港 CPI",
    "macro_china_hk_cpi_ratio": "香港 CPI 同比",
    "macro_china_hk_ppi": "香港 PPI",
    "macro_china_hk_gbp": "香港 GDP",
    "macro_china_hk_gbp_ratio": "香港 GDP 同比",
    "macro_china_hk_rate_of_unemployment": "香港失业率",
    "macro_china_hk_trade_diff_ratio": "香港贸易差额",
    "macro_china_hk_building_amount": "香港建筑工程金额",
    "macro_china_hk_building_volume": "香港建筑工程量",
}


CNINFO_CATEGORIES = {
    "": "全部", "category_ndbg_szsh": "年报", "category_bndbg_szsh": "半年报",
    "category_yjdbg_szsh": "一季报", "category_sjdbg_szsh": "三季报",
    "category_yjygjxz_szsh": "业绩预告", "category_qyfpxzcs_szsh": "权益分派",
    "category_dshgg_szsh": "董事会", "category_jshgg_szsh": "监事会",
    "category_gddh_szsh": "股东会", "category_rcjy_szsh": "日常经营",
    "category_gszl_szsh": "公司治理", "category_zj_szsh": "中介报告",
    "category_sf_szsh": "首发", "category_zf_szsh": "增发",
    "category_gqjl_szsh": "股权激励", "category_pg_szsh": "配股",
    "category_jj_szsh": "解禁", "category_gszq_szsh": "公司债",
    "category_kzzq_szsh": "可转债", "category_qtrz_szsh": "其他融资",
    "category_gqbd_szsh": "股权变动", "category_bcgz_szsh": "补充更正",
    "category_cqdq_szsh": "澄清致歉", "category_fxts_szsh": "风险提示",
    "category_tbclts_szsh": "特别处理和退市", "category_tszlq_szsh": "退市整理期",
}

# 第 5 位是该分类的客户端过滤词（标题或长标题须包含；空为不过滤）。
# 業績公告在 titleSearchServlet 里没有独立子码（2026-11-05 实测 t2Gcode/t2code
# 组合均被当不过滤处理），用宽查加「業績」客户端筛实现，与公告及通告区分开。
HKEX_CATEGORIES = {
    "all": ("-2", "-2", "-2", "全部", ""),
    "annual": ("40000", "-1", "40100", "年報", ""),
    "interim": ("40000", "-1", "40200", "中期/半年度報告", ""),
    "quarterly": ("40000", "-1", "40300", "季度報告", ""),
    "esg": ("40000", "-1", "40400", "ESG 資料/報告", ""),
    "results": ("10000", "-2", "-2", "業績公告", "業績"),
    "notice": ("10000", "-2", "-2", "公告及通告", ""),
}

PBC_PAGES = {
    "货币统计概览": f"{PBC_STATS_BASE}/2026ntjsj/hbtjgl/index.html",
    "金融业机构资产负债统计": f"{PBC_STATS_BASE}/2026ntjsj/jryjgzcfztj/index.html",
    "金融机构信贷收支统计": f"{PBC_STATS_BASE}/2026ntjsj/jrjgxdsztj/index.html",
    "金融市场统计": f"{PBC_STATS_BASE}/2026ntjsj/jrsctj/index.html",
}

CPCA_CHARTS = {
    1: "整体市场产销（产量/批发/零售/出口）",
    2: "厂商销量（批发/零售）",
    3: "车型大类（MPV/SUV/轿车）",
    4: "国别份额",
    5: "级别份额",
    6: "新能源市场（产量/批发/零售/出口）",
}

CPCA_METRICS = {
    1: ["产量", "批发", "零售", "出口"],
    2: ["批发", "零售"],
    3: ["产量", "批发", "零售", "出口"],
}



class FetchError(RuntimeError):
    pass


def _enum_or_raise(value, keys, name, hint=None):
    """枚举参数容错解析，返回 keys 里的原始键。
    模型会把选项列表或标签拼进取值（"6新能源"、"1行业"、"interim|q1"），
    逐段拆开匹配、取开头数字匹配、按 dict 值反查，都不中才报错，报错带原值。"""
    s = str(value if value is not None else "").strip()
    norm = {str(k): k for k in keys}
    if s in norm:
        return norm[s]
    for part in re.split(r"[|/、,，;；\s]+", s):
        part = part.strip()
        if not part:
            continue
        if part in norm:
            return norm[part]
        m = re.match(r"^(\d+)(.*)$", part)
        if m:
            num, rest = m.group(1), m.group(2).strip()
            if num in norm:
                return norm[num]
            if num.lstrip("0") in norm:
                return norm[num.lstrip("0")]
            if rest in norm:
                return norm[rest]
    if isinstance(keys, dict):
        for k, v in keys.items():
            if s == str(v):
                return k
    raise FetchError(hint or f"{name} 只能是 {'、'.join(norm)} 之一，收到的是「{s}」")


def _cfg():
    return config.CFG.get("fetcher") or {}


def _timeout():
    return float(_cfg().get("timeout_s") or 30)


def _delay():
    return float(_cfg().get("delay_s") or 1.5)


def _client(headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    return httpx.Client(headers=h, timeout=_timeout(), follow_redirects=True)


def _conn_hint(e):
    text = str(e)
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)) or "SSL" in text:
        text += "（如开着 VPN 或系统代理，请关闭后重试）"
    return text


def _get_json(url, params=None, attempts=3):
    last = None
    for i in range(attempts):
        try:
            with _client() as c:
                r = c.get(url, params=params)
                r.raise_for_status()
                return r.json()
        except (httpx.HTTPError, ValueError) as e:
            last = e
            time.sleep(1 + i)
    raise FetchError(f"接口请求失败：{url}（{_conn_hint(last)}）")


def _post_json(url, payload, attempts=3):
    last = None
    for i in range(attempts):
        try:
            with _client() as c:
                r = c.post(url, json=payload)
                r.raise_for_status()
                return r.json()
        except (httpx.HTTPError, ValueError) as e:
            last = e
            time.sleep(1 + i)
    raise FetchError(f"接口请求失败：{url}（{_conn_hint(last)}）")


_WESTERN_CHARSETS = {"iso-8859-1", "latin-1", "windows-1252", "cp1252", "us-ascii", "ascii"}


def _declared_charset(resp):
    """响应头或页面 meta 里声明的编码，取到了就先按它解。"""
    headers = getattr(resp, "headers", None) or {}
    ctype = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
    m = re.search(r"charset=[\"']?([\w-]+)", str(ctype), re.I)
    if m:
        return m.group(1)
    head = resp.content[:4096].decode("ascii", "ignore")
    m = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.I)
    return m.group(1) if m else ""


def _decode(resp):
    """按声明编码→utf-8→gb18030 依次解，gb18030 是 GBK 的超集，能接住扩展字。

    只用 gbk 兜底时，混合编码或 GB18030 扩展字会让整段退到带替换解码，
    快照里出现成片「����」，审校拿乱码当锚点原文核不出数字。"""
    raw = resp.content
    order = []
    declared = _declared_charset(resp)
    # 声明成单字节西欧编码的多半是服务器默认值（实际常是 gb2312/gbk），
    # 照它解不会报错，只会把整页解成乱码，留给后面的 utf-8/gb18030
    if declared and declared.lower().replace("_", "-") not in _WESTERN_CHARSETS:
        order.append(declared)
    order += ["utf-8", "gb18030"]
    if declared:
        order.append(declared)
    for enc in dict.fromkeys(order):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _cell_text(html):
    html = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s+", " ", html).strip()


def _strip_group_prefix(name):
    return re.sub(r"^（[A-Za-z]）", "", str(name or "")).strip()


# ---------- 慧博榜单 ----------

def _parse_author_rows(html):
    """解析新财富最佳分析师榜单页：行业组名在每组第一格的 <td font-weight:bold> 里，后面是同组的机构与分析师。"""
    items = []
    group = ""
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 4:
            continue
        name = _cell_text(tds[0])
        if name:
            group = name
        rank_text = _cell_text(tds[1])
        if not group or not rank_text.isdigit():
            continue
        analysts = [a.strip() for a in re.split(r"[，,、]", _cell_text(tds[3])) if a.strip()]
        items.append({
            "group": group,
            "industry": _strip_group_prefix(group),
            "rank": int(rank_text),
            "org": _cell_text(tds[2]),
            "analysts": analysts,
        })
    return items


def fetch_hibor_ranking(year=None, bigtype=2):
    year = int(year or (datetime.now().year - 1))
    bigtype = _enum_or_raise(bigtype or 2, HIBOR_BIGTYPE, "慧博榜单类型")
    params = {"year": year, "bigtype": bigtype}
    headers = {"Referer": "https://www.hibor.com.cn/", "Accept-Language": "zh-CN,zh;q=0.9",
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    last = ""
    for attempt in range(2):
        try:
            with _client(headers) as c:
                r = c.get(HIBOR_AUTHOR_URL, params=params)
                r.raise_for_status()
                html = _decode(r)
            items = _parse_author_rows(html)
            if items:
                return {"year": year, "bigtype": bigtype, "label": HIBOR_BIGTYPE[bigtype],
                        "url": str(r.url), "items": items}
            last = "页面没有可解析的榜单行"
        except httpx.HTTPError as e:
            last = _conn_hint(e)
        if attempt == 0:
            time.sleep(3)
    raise FetchError(f"慧博 {year} 年{HIBOR_BIGTYPE[bigtype]}榜单解析为空（{last}）；"
                     "该站有频率限制，稍等再试，或到会话里让资料员看一眼")


def _industry_terms(industry):
    t = str(industry or "").strip()
    terms = [t] if t else []
    alias = INDUSTRY_ALIASES.get(t)
    if alias and alias not in terms:
        terms.append(alias)
    return terms


def _match_score(industry, group):
    """行业与榜单组名的贴合度：包含最强，二字词重合次之，单字重合兜底（乘用车→汽车靠「车」）。"""
    a, b = str(industry or ""), _strip_group_prefix(group)
    if not a or not b:
        return 0
    if a in b or b in a:
        return 100 + min(len(a), len(b))
    grams_a = {a[i:i + 2] for i in range(len(a) - 1)}
    grams_b = {b[i:i + 2] for i in range(len(b) - 1)}
    bigrams = len(grams_a & grams_b)
    if bigrams:
        return bigrams * 3
    return len(set(a) & set(b))


MATCH_PROMPT = """你是研报资料员。下面是新财富最佳分析师榜单的行业组名单（每组一个冠军团队）：
{GROUPS}

用户要写「{INDUSTRY}」行业的研报，请从名单里挑一个最贴近的行业组。只输出 JSON，不要解释：
{{"group": "组名原文", "reason": "一句话"}}
名单里确实没有相近的行业组时，group 输出空字符串。"""


def _llm_match_group(items, industry):
    groups = sorted({it["group"] for it in items})
    try:
        out = llm.chat(
            [{"role": "user", "content": MATCH_PROMPT.replace("{GROUPS}", "\n".join(groups)).replace("{INDUSTRY}", industry)}],
            task="classify", response_json=True,
        )
    except Exception:
        return None
    picked = str(out.get("group") or "").strip()
    return picked if picked in groups else None


def match_group(items, industry, use_llm=True):
    """按行业名找榜单里最贴近的行业组：先按字面重合挑，完全对不上再让 LLM 从组名单里选。"""
    groups = {it["group"] for it in items}
    if not groups:
        return None
    terms = _industry_terms(industry)
    best_score, best = max(
        ((max(_match_score(t, g) for t in terms), g) for g in groups),
        key=lambda x: x[0],
    )
    # 单字重合（用/车/电这类）太弱，宁可交给 LLM 选，也不硬配一个不相干的组
    if best_score >= 3:
        return best
    if not use_llm:
        return None
    return _llm_match_group(items, industry)


def match_champion(items, industry, use_llm=True):
    """匹配行业组后取该组冠军，保留给只需要一个标杆的旧调用。"""
    group = match_group(items, industry, use_llm=use_llm)
    if not group:
        return None
    return next((it for it in items if it["group"] == group and it.get("rank") == 1), None)


# ---------- 东方财富研报 ----------

EASTMONEY_QTYPE = {"0": "个股研报", "1": "行业研报", "2": "策略报告"}
EASTMONEY_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"


def eastmoney_stock_code(name):
    """A 股名称→6 位代码（suggest 接口免登录）。只认 Classify=AStock，精确名优先，
    解析不到时报错提示直接填代码。返回 (代码, 名称)。"""
    name = str(name or "").strip()
    if re.fullmatch(r"\d{6}", name):
        return name, ""
    try:
        data = _get_json(EASTMONEY_SUGGEST_URL,
                         params={"input": name, "type": "14", "count": "8"})
    except FetchError as e:
        raise FetchError(f"东财代码检索失败，请直接填 6 位股票代码（{e}）")
    rows = ((data.get("QuotationCodeTable") or {}).get("Data")) or []
    a_stocks = [it for it in rows if str(it.get("Classify") or "") == "AStock"
                and re.fullmatch(r"\d{6}", str(it.get("Code") or ""))]
    exact = [it for it in a_stocks if str(it.get("Name") or "") == name]
    hits = exact or a_stocks
    if not hits:
        raise FetchError(f"东财没有解析到 A 股「{name}」，请直接填 6 位股票代码，如 002594")
    return str(hits[0]["Code"]), str(hits[0].get("Name") or "")


def fetch_eastmoney_reports(industry=None, broker=None, limit=3, months=24, brokers_only=False,
                            qtype=1, stock_code="", industry_code="", org_code="",
                            rating_change="", page=1):
    limit = max(1, min(int(limit or 3), int(_cfg().get("eastmoney_max") or 3)))
    qtype = _enum_or_raise(qtype if qtype is not None else 1, EASTMONEY_QTYPE,
                           "东财研报 qtype（0=个股，1=行业，2=策略）")
    resolved = ""
    if str(qtype) == "0":
        stock_code = str(stock_code or "").strip()
        if stock_code and not re.fullmatch(r"\d{6}", stock_code):
            code, cname = eastmoney_stock_code(stock_code)
            resolved = f"{stock_code} → {code}" + (f"（{cname}）" if cname else "")
            stock_code = code
    end = datetime.now()
    begin = end - timedelta(days=int(months or 24) * 31)
    params = {
        "industryCode": str(industry_code or "*"), "pageSize": 100, "industry": "*",
        "rating": "", "ratingChange": str(rating_change or ""),
        "beginTime": begin.strftime("%Y-%m-%d"), "endTime": end.strftime("%Y-%m-%d"),
        "pageNo": max(1, int(page or 1)), "fields": "", "qType": qtype,
        "orgCode": str(org_code or ""), "code": str(stock_code or "*"), "rcode": "",
        "p": max(1, int(page or 1)), "pageNum": max(1, int(page or 1)),
        "pageNumber": max(1, int(page or 1)),
    }
    data = _get_json(EASTMONEY_LIST_URL, params=params)
    rows = data.get("data") or []
    terms = _industry_terms(industry)
    picked = []
    for it in rows:
        title = str(it.get("title") or "")
        org = str(it.get("orgSName") or it.get("orgName") or "")
        ind = str(it.get("industryName") or "")
        if terms and qtype != "2" and not any(t in ind or t in title for t in terms):
            continue
        if broker and broker not in org and broker not in str(it.get("orgName") or ""):
            continue
        if brokers_only and "证券" not in org and it.get("orgType") != "white":
            continue
        if not it.get("infoCode"):
            continue
        picked.append(it)

    def _depth_key(it):
        title = str(it.get("title") or "")
        deep = 0 if ("深度" in title or "专题" in title) else 1
        return (deep, -(it.get("attachPages") or 0), -(it.get("count") or 0))

    picked.sort(key=_depth_key)
    picked = picked[:limit]
    if not picked:
        target = industry or stock_code or "该条件"
        who = f"{broker}的" if broker else ""
        raise FetchError(f"东财研报中心没找到{who}「{target}」近 {months} 个月的"
                         f"{EASTMONEY_QTYPE[qtype]}（第 {page} 页）")

    downloaded, skipped = [], []
    for i, it in enumerate(picked, 1):
        title = str(it.get("title") or "")
        org = str(it.get("orgSName") or it.get("orgName") or "")
        date = str(it.get("publishDate") or "")[:10].replace("-", "")
        stem = sanitize(f"{org}_{title}")[:60] or str(it["infoCode"])
        url = EASTMONEY_PDF_URL.format(info_code=it["infoCode"])
        try:
            dest = _download_pdf(url, stem)
        except FetchError as e:
            skipped.append({"title": title, "org": org, "error": str(e)})
            continue
        downloaded.append({
            "title": title, "org": org, "date": date, "industry": it.get("industryName") or "",
            "rating": it.get("emRatingName") or it.get("ratingName") or "",
            "eps": it.get("predictThisYearEps") or it.get("predictNextYearEps") or "",
            "pe": it.get("predictThisYearPe") or it.get("predictNextYearPe") or "",
            "count": it.get("count"), "pages": it.get("attachPages"),
            "file": dest.name, "url": url,
        })
        if i < len(picked):
            time.sleep(_delay())
    if not downloaded:
        raise FetchError("研报都下载失败了：" + "；".join(f"{s['title']}（{s['error']}）" for s in skipped))
    return {"items": downloaded, "skipped": skipped, "url": EASTMONEY_LIST_URL,
            "resolved": resolved}


def _download_pdf(url, stem):
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.INBOX_DIR / f".{stem}.{time.time_ns()}.part"
    try:
        with _client() as c:
            with c.stream("GET", url) as r:
                r.raise_for_status()
                first = True
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes():
                        if first:
                            if not chunk.startswith(b"%PDF"):
                                raise FetchError("下载内容不是 PDF（链接失效或需要登录）")
                            first = False
                        f.write(chunk)
        if not tmp.stat().st_size:
            raise FetchError("下载到的 PDF 是空文件")
        dup = find_duplicate(tmp)
        if dup is not None:
            sha = sha256_of(tmp)
            entry = next((f for f in ingest_mod.load_manifest()["files"]
                          if f.get("sha256") == sha), None)
            if entry is None:
                raise FetchError(f"内容与已有文件重复、且尚未摄入，先处理库里那份："
                                 f"{dup.relative_to(config.ROOT).as_posix()}")
            # 已摄入的重复件直接复用库内文件：缺口取数拿它照样有锚点，不算失败
            tmp.unlink(missing_ok=True)
            return dup
        name = unique_target(config.INBOX_DIR, f"{stem}.pdf")
        dest = config.INBOX_DIR / name
        tmp.replace(dest)
        return dest
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ---------- 国家统计局 ----------

FREQ_CODE = {"月": "1", "季": "2", "年": "3"}
STATS_FREQ_LABEL = {"MM": "月", "SS": "季", "YY": "年"}

PROVINCES = ["北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
             "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
             "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
             "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆"]

# 预置指标（2026-11-05 实测 data.stats.gov.cn external 接口的口径矩阵）：
# CPI/PPI/社零 全国与 31 省均只有年频；GDP 全国有季频（累计/当季/环比三支）与年频、
# 省级仅年频；PMI 仅全国，月频为主。prefer 是同名多序列时的优先口径
# （CPI/PPI 取上年=100 而非 1978=100 定基，GDP 季频取累计值）。
STATS_PRESETS = {
    "居民消费价格指数（CPI）": {
        "search": "居民消费价格", "main": "居民消费价格指数",
        "regions": ["全国"] + PROVINCES, "freqs": {"*": ["年"]},
        "prefer": {"年": "上年=100"}},
    "工业生产者出厂价格指数（PPI）": {
        "search": "工业生产者出厂价格", "main": "工业生产者出厂价格指数",
        "regions": ["全国"] + PROVINCES, "freqs": {"*": ["年"]},
        "prefer": {"年": "上年=100"}},
    "国内生产总值（GDP）": {
        "search": "国内生产总值", "main": "国内生产总值",
        "regions": ["全国"] + PROVINCES,
        "freqs": {"全国": ["季", "年"], "*": ["年"]},
        "prefer": {"季": "累计值"}},
    "社会消费品零售总额": {
        "search": "社会消费品零售总额", "main": "社会消费品零售总额",
        "regions": ["全国"] + PROVINCES, "freqs": {"*": ["年"]}},
    "制造业采购经理指数（PMI）": {
        "search": "制造业采购经理指数", "main": "制造业采购经理指数",
        "regions": ["全国"], "freqs": {"*": ["月", "年"]}},
}


def _dts_range(dt, dt_type, periods):
    periods = max(1, min(int(periods or 24), 120))
    dt = str(dt or "")
    if dt_type == "YY" or len(dt) <= 4:
        end = int(dt[:4] or datetime.now().year)
        return f"{end - periods + 1}YY-{end}YY"
    if dt_type == "SS":
        q = int(dt[4:6]) if dt[4:6].isdigit() and 1 <= int(dt[4:6]) <= 4 else 4
        end = int(dt[:4]) * 4 + (q - 1)
        start = end - periods + 1
        sy, sq = divmod(start, 4)
        ey, eq = divmod(end, 4)
        return f"{sy}{sq + 1:02d}SS-{ey}{eq + 1:02d}SS"
    year, month = int(dt[:4]), int(dt[4:6] or 12)
    end = year * 12 + month - 1
    start = end - periods + 1
    sy, sm = divmod(start, 12)
    ey, em = divmod(end, 12)
    return f"{sy}{sm + 1:02d}MM-{ey}{em + 1:02d}MM"


def _nbs_search(keyword, page_size=5):
    res = _get_json(f"{NBS_BASE}/query",
                    params={"search": keyword, "pagenum": 1, "pageSize": page_size})
    data = res.get("data")
    if isinstance(data, dict):
        data = data.get("data") or data.get("list") or []
    return data or []


def _nbs_one(keyword, freq, periods, region, strict_main="", prefer=None):
    search = keyword if region in ("", "全国") else f"{region} {keyword}"
    items = _nbs_search(search, page_size=10)
    if not items:
        raise FetchError(f"统计局接口没有搜到「{search}」，换个更短的中文关键词再试")
    want = FREQ_CODE.get(freq)
    freq_hits = [x for x in items if want and str(x.get("type_value")) == want]
    pool = freq_hits or items
    # 预置路径：频率命中时再按主序列名前缀收紧（如只认「居民消费价格指数」开头的），
    # 规避搜索相关性把同名分项排前的坑；收紧落空回退宽松池，实际序列名与频率在结果里如实标注
    if strict_main and want and freq_hits:
        strict = [x for x in freq_hits
                  if str(x.get("show_name") or "").split("(")[0].strip().startswith(strict_main)]
        if strict:
            pool = strict
    if region not in ("", "全国"):
        region_hits = [x for x in pool if region in str(x.get("da_name") or "")]
        if region_hits:
            pool = region_hits
        else:
            raise FetchError(f"统计局搜到「{keyword}」，但没有「{region}」口径；"
                             "该指标可能只发布全国数")
    if prefer:
        hits = [x for x in pool if prefer in str(x.get("show_name") or "")]
        if hits:
            pool = hits

    def _name_score(x):
        name = str(x.get("show_name") or "").strip()
        return (2 if name.startswith(keyword) else 0) + (1 if name.split("(")[0].strip() == keyword else 0)

    chosen = max(pool, key=_name_score)
    cid, indic_id = chosen.get("cid"), chosen.get("indic_id")
    if not cid or not indic_id:
        raise FetchError("统计局搜索结果缺少 cid / indicatorId，接口结构可能变了")
    dt_type = str(chosen.get("dt_type") or "MM")
    dts = _dts_range(chosen.get("dt"), dt_type, periods)
    da_name = str(chosen.get("da_name") or region or "全国").strip()
    da_code = str(chosen.get("da") or "000000000000").strip()
    payload = {"cid": cid, "indicatorIds": [indic_id],
               "das": [{"text": da_name, "value": da_code}],
               "dts": [dts], "showType": "1"}
    res = _post_json(f"{NBS_BASE}/stream/esData", payload)
    rows = []
    for block in res.get("data") or []:
        for v in block.get("values") or []:
            val = str(v.get("value") or "").strip()
            if not val or val in ("（未发布）", "nan", "None"):
                continue
            rows.append({
                "指标": str(v.get("i_showname") or chosen.get("show_name") or keyword).strip(),
                "数值": val,
                "单位": str(v.get("du_name") or "").replace("无", "") or "—",
                "口径": " · ".join(x for x in (da_name, str(chosen.get("show_name") or keyword).strip()) if x),
                "时点": str(block.get("name") or block.get("code") or ""),
            })
    rows.reverse()
    if not rows:
        raise FetchError(f"统计局「{da_name} · {chosen.get('show_name') or keyword}」返回空数据"
                         "（可能未发布，或口径编码变了）")
    return {"keyword": keyword, "indicator": chosen.get("show_name"), "region": da_name,
            "dt_type": dt_type, "dts": dts, "rows": rows}


def fetch_stats_series(keyword, freq="auto", periods=24, region="全国"):
    keyword = str(keyword or "").strip()
    if not keyword:
        raise FetchError("统计局取数需要中文指标关键词，如 居民消费价格")
    freq = _enum_or_raise(freq or "auto", ("auto", "月", "季", "年"), "统计局频率")
    region = str(region or "全国").strip() or "全国"
    keywords = [k.strip() for k in re.split(r"[;；,，]", keyword) if k.strip()]
    preset = STATS_PRESETS.get(keywords[0]) if len(keywords) == 1 else None
    strict_main, prefer = "", None
    if preset:
        if region not in preset["regions"]:
            raise FetchError(f"「{keywords[0]}」没有「{region}」口径，可选地区："
                             f"{'、'.join(preset['regions'])}")
        allowed = preset["freqs"].get(region) or preset["freqs"].get("*") or []
        if freq == "auto":
            freq = allowed[0] if allowed else "auto"
        elif freq not in allowed:
            raise FetchError(f"「{keywords[0]}」的{region}口径只有 {'、'.join(allowed)} 频，"
                             f"收到的是 {freq}")
        strict_main = preset["main"]
        prefer = (preset.get("prefer") or {}).get(freq)
    all_rows, names, dts_list, freqs_seen = [], [], [], []
    for kw in keywords:
        search_kw = preset["search"] if preset else kw
        one = _nbs_one(search_kw, freq, periods, region,
                       strict_main=strict_main, prefer=prefer)
        all_rows.extend(one["rows"])
        names.append(str(one["indicator"] or kw))
        if one["dts"] not in dts_list:
            dts_list.append(one["dts"])
        lbl = STATS_FREQ_LABEL.get(str(one.get("dt_type") or ""), "")
        if lbl and lbl not in freqs_seen:
            freqs_seen.append(lbl)
    return {"keyword": "、".join(keywords), "indicator": "、".join(names),
            "region": region, "dt_type": "auto", "freq": "、".join(freqs_seen),
            "dts": "；".join(dts_list), "rows": all_rows,
            "url": f"{NBS_BASE}/stream/esData"}


# ---------- 巨潮公告 ----------

def _clean_title(s):
    return re.sub(r"<[^>]+>", "", str(s or "")).strip()


_CNINFO_STOCKS = None


def _cninfo_stock_map():
    global _CNINFO_STOCKS
    if _CNINFO_STOCKS is None:
        data = _get_json(CNINFO_STOCK_URL)
        rows = data.get("stockList") or data.get("data") or []
        _CNINFO_STOCKS = rows
    return _CNINFO_STOCKS


def cninfo_stock(code="", name=""):
    code = str(code or "").strip()
    name = str(name or "").strip()
    rows = _cninfo_stock_map()
    if code:
        hit = next((x for x in rows if str(x.get("code") or "") == code), None)
        if hit:
            return hit
    if name:
        hit = next((x for x in rows if str(x.get("zwjc") or "") == name), None)
        if hit:
            return hit
        hit = next((x for x in rows if name in str(x.get("zwjc") or "")), None)
        if hit:
            return hit
    return None


def _cninfo_search(query, page_size=10, category="", stock="", column="szse", se_date=""):
    form = {"pageNum": 1, "pageSize": max(int(page_size or 10), 10), "column": column or "szse",
            "tabName": "fulltext", "plate": "", "stock": stock, "searchkey": query,
            "seDate": se_date, "tdate": "", "type": "", "category": category,
            "sortName": "", "sortType": "", "isHLtitle": "true"}
    try:
        with _client() as c:
            r = c.post(CNINFO_QUERY_URL, data=form,
                       headers={"Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"})
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise FetchError(f"巨潮公告接口请求失败：{_conn_hint(e)}")
    return data.get("announcements") or []


def fetch_cninfo_announce(query, limit=5, code="", category="", months=0, column="szse"):
    query = str(query or "").strip()
    code = str(code or "").strip()
    if category:
        category = _enum_or_raise(category, CNINFO_CATEGORIES, "巨潮公告分类",
                                  hint=f"未知巨潮公告分类：{category}")
    stock = ""
    if code:
        if code.upper().endswith(".HK"):
            raise FetchError(f"「{code}」是港股代码，巨潮只覆盖 A 股（沪深北）；"
                             "请改用「公司年报」或「港交所披露易·公告」")
        hit = cninfo_stock(code=code)
        if not hit:
            raise FetchError(f"巨潮股票列表里没有代码「{code}」；"
                             "巨潮只覆盖 A 股（沪深北），港股请改用「公司年报」或「港交所披露易·公告」")
        stock = f"{hit.get('code')},{hit.get('orgId')}"
        query = query or ""
    if not query and not stock:
        raise FetchError("巨潮公告检索需要关键词或股票代码，如 比亚迪 / 002594")
    limit = max(1, min(int(limit or 5), 10))
    se_date = ""
    if int(months or 0) > 0:
        end = datetime.now()
        begin = end - timedelta(days=int(months) * 31)  # 月按 31 天折算，与东财口径一致
        se_date = f"{begin:%Y-%m-%d}~{end:%Y-%m-%d}"
    items = _cninfo_search(query, page_size=max(limit * 3, 30), category=category,
                           stock=stock, column=column, se_date=se_date)
    if not items:
        label = CNINFO_CATEGORIES.get(category, category) if category else "公告"
        raise FetchError(f"巨潮没有搜到「{query or code}」的{label}。"
                         "巨潮只覆盖 A 股（沪深北）公告，港股（如吉利汽车 00175.HK）"
                         "请改用「公司年报」（code 填 0175.HK）或「港交所披露易·公告」")

    downloaded, skipped = [], []
    for it in items:
        if len(downloaded) >= limit:
            break
        title = _clean_title(it.get("announcementTitle"))
        sec = _clean_title(it.get("secName"))
        adjunct = str(it.get("adjunctUrl") or "").strip()
        if not adjunct:
            skipped.append({"title": title, "error": "没有附件地址"})
            continue
        url = adjunct if adjunct.startswith("http") else CNINFO_PDF_BASE + adjunct.lstrip("/")
        ts = it.get("announcementTime")
        try:
            date = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y%m%d")
        except (TypeError, ValueError):
            date = ""
        stem = sanitize(f"{sec}_{title}")[:60] or "公告"
        try:
            dest = _download_pdf(url, stem)
        except FetchError as e:
            skipped.append({"title": title, "error": str(e)})
            continue
        downloaded.append({"title": title, "org": sec, "date": date,
                           "category": CNINFO_CATEGORIES.get(category, category),
                           "file": dest.name, "url": url})
        time.sleep(_delay())
    if not downloaded:
        raise FetchError("公告都下载失败了：" + "；".join(f"{s['title']}（{s['error']}）" for s in skipped))
    return {"items": downloaded, "skipped": skipped, "url": CNINFO_QUERY_URL}


# ---------- 公司年报 ----------

def _annual_year(title):
    m = re.search(r"(20\d{2})\s*年?\s*年度报告", str(title or ""))
    return int(m.group(1)) if m else 0


def _pick_cninfo_annual(items, company, year=None):
    picked = []
    for it in items:
        title = _clean_title(it.get("announcementTitle"))
        sec = _clean_title(it.get("secName"))
        if "年度报告" not in title or "摘要" in title or "英文" in title:
            continue
        if company not in sec and company not in title:
            continue
        y = _annual_year(title)
        if year and y != int(year):
            continue
        picked.append((y, it))
    picked.sort(key=lambda x: x[0], reverse=True)
    return [it for _, it in picked]


def fetch_cninfo_annual(company, year=None, limit=1):
    company = str(company or "").strip()
    if not company:
        raise FetchError("取年报需要公司名")
    items = _cninfo_search(f"{company} 年度报告", page_size=30, category="category_ndbg_szsh")
    picked = _pick_cninfo_annual(items, company, year)
    if not picked:
        items = _cninfo_search(f"{company} 年度报告", page_size=30)
        picked = _pick_cninfo_annual(items, company, year)
    if not picked:
        raise FetchError(f"巨潮没有搜到「{company}」的年度报告（可能是港股，改走 HKEXnews）")
    downloaded, skipped = [], []
    for it in picked[:max(1, int(limit or 1))]:
        title = _clean_title(it.get("announcementTitle"))
        sec = _clean_title(it.get("secName"))
        adjunct = str(it.get("adjunctUrl") or "").strip()
        if not adjunct:
            skipped.append({"title": title, "error": "没有附件地址"})
            continue
        url = adjunct if adjunct.startswith("http") else CNINFO_PDF_BASE + adjunct.lstrip("/")
        ts = it.get("announcementTime")
        try:
            date = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y%m%d")
        except (TypeError, ValueError):
            date = ""
        y = _annual_year(title)
        stem = sanitize(f"{company}_{y}年度报告" if y else f"{company}_年度报告")[:60]
        try:
            dest = _download_pdf(url, stem)
        except FetchError as e:
            skipped.append({"title": title, "error": str(e)})
            continue
        downloaded.append({"title": title, "org": sec, "date": date, "file": dest.name, "url": url})
        time.sleep(_delay())
    if not downloaded:
        raise FetchError("年度报告下载失败：" + "；".join(f"{s['title']}（{s['error']}）" for s in skipped))
    return {"items": downloaded, "skipped": skipped, "url": CNINFO_QUERY_URL}


CNINFO_PERIODIC = {
    "interim": ("category_bndbg_szsh", ("半年度报告", "半年报"), "半年度报告"),
    "q1": ("category_yjdbg_szsh", ("第一季度报告", "一季度报告", "一季报"), "第一季度报告"),
    "q3": ("category_sjdbg_szsh", ("第三季度报告", "三季度报告", "三季报"), "第三季度报告"),
}


def _pick_periodic(items, company, patterns):
    """只留正式全文：公司对得上、标题含报告名、跳过摘要英文更正稿。"""
    picked = []
    for it in items:
        title = _clean_title(it.get("announcementTitle"))
        sec = _clean_title(it.get("secName"))
        if company not in sec and company not in title:
            continue
        if not any(p in title for p in patterns):
            continue
        if any(x in title for x in ("摘要", "英文", "更正", "补充", "取消", "已取消")):
            continue
        picked.append(it)
    return picked


def fetch_cninfo_periodic(company, kind="interim", days=400, limit=1, code=""):
    """取 A 股半年报或季报。只留正式全文，跳过摘要、英文版与更正稿。

    关键词全文检索按相关度排序，常只捞到摘要捞不到全文（宇通 2026 半年报全文
    就被漏掉）；筛不出全文时改按股票代码拉该公司时间窗内的公告再筛标题。"""
    company = str(company or "").strip()
    kind = _enum_or_raise(str(kind or "interim").lower(), CNINFO_PERIODIC,
                          "A 股定期报告类型", hint=f"未知定期报告类型：{kind}")
    if not company:
        raise FetchError("取定期报告需要公司名")
    category, patterns, label = CNINFO_PERIODIC[kind]
    begin = datetime.now() - timedelta(days=int(days or 400))
    se_date = f"{begin:%Y-%m-%d}~{datetime.now():%Y-%m-%d}"
    items = _cninfo_search(company, page_size=50, category=category, se_date=se_date)
    picked = _pick_periodic(items, company, patterns)
    if not picked:
        stock = cninfo_stock(code=str(code or "").strip()) or cninfo_stock(name=company)
        if stock:
            sid = f"{stock.get('code')},{stock.get('orgId')}"
            items = _cninfo_search("", page_size=50, category=category, stock=sid, se_date=se_date)
            picked = _pick_periodic(items, company, patterns)
    if not picked:
        # 时间窗内筛不出时退回无窗口检索，宁可取到旧一期全文也不空手（旧行为）
        items = _cninfo_search(company, page_size=50, category=category)
        picked = _pick_periodic(items, company, patterns)
    if not picked:
        raise FetchError(f"巨潮没有搜到「{company}」的{label}（可能是港股，改走 HKEXnews）")

    def _ts(it):
        try:
            return int(it.get("announcementTime") or 0)
        except (TypeError, ValueError):
            return 0

    picked.sort(key=_ts, reverse=True)
    downloaded, skipped = [], []
    for it in picked[:max(1, int(limit or 1))]:
        title = _clean_title(it.get("announcementTitle"))
        sec = _clean_title(it.get("secName"))
        adjunct = str(it.get("adjunctUrl") or "").strip()
        if not adjunct:
            skipped.append({"title": title, "error": "没有附件地址"})
            continue
        url = adjunct if adjunct.startswith("http") else CNINFO_PDF_BASE + adjunct.lstrip("/")
        ts = it.get("announcementTime")
        try:
            date = datetime.fromtimestamp(int(ts) / 1000).strftime("%Y%m%d")
        except (TypeError, ValueError):
            date = ""
        m = re.search(r"(20\d{2})", title)
        year = m.group(1) if m else ""
        stem = sanitize(f"{company}_{year}{label}")[:60]
        try:
            dest = _download_pdf(url, stem)
        except FetchError as e:
            skipped.append({"title": title, "error": str(e)})
            continue
        downloaded.append({"title": title, "org": sec, "date": date,
                           "category": label, "file": dest.name, "url": url})
        time.sleep(_delay())
    if not downloaded:
        raise FetchError(f"{label}下载失败：" + "；".join(f"{s['title']}（{s['error']}）" for s in skipped))
    return {"items": downloaded, "skipped": skipped, "url": CNINFO_QUERY_URL}


def _parse_hkex_prefix(text):
    text = str(text or "").strip()
    m = re.search(r"\((\{.*\})\)\s*;?\s*$", text, re.S)
    payload = json.loads(m.group(1) if m else text)
    return payload.get("stockInfo") or []


def _parse_hkex_reports(payload):
    rows = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(rows, str):
        rows = json.loads(rows)
    return rows or []


def _hkex_stock(company, code=""):
    name = str(code or "").split(".")[0].strip() or str(company or "").strip()
    if not name:
        raise FetchError("取港股年报需要公司名或股票代码")
    params = {"callback": "callback", "lang": "ZH", "type": "A", "name": name, "market": "SEHK"}
    try:
        with _client() as c:
            r = c.get(HKEX_PREFIX_URL, params=params)
            r.raise_for_status()
            rows = _parse_hkex_prefix(_decode(r))
    except (httpx.HTTPError, ValueError) as e:
        raise FetchError(f"HKEXnews 股票检索失败：{_conn_hint(e)}")
    if not rows:
        raise FetchError(f"HKEXnews 没有搜到「{name}」")
    want = str(code or "").split(".")[0].strip().lstrip("0")
    if want:
        for it in rows:
            if str(it.get("code") or "").lstrip("0") == want:
                return it
    for it in rows:
        if str(it.get("name") or "") == str(company or ""):
            return it
    return rows[0]


def fetch_hkex_annual(company, code="", year=None, limit=1):
    stock = _hkex_stock(company, code)
    now_year = datetime.now().year
    params = {
        "sortDir": 0, "sortByOptions": "DateTime", "category": 0, "market": "SEHK",
        "stockId": stock["stockId"], "documentType": -1,
        "fromDate": f"{now_year - 4}0101", "toDate": f"{now_year}1231",
        "title": "", "searchType": 1, "t1code": 40000, "t2Gcode": -2, "t2code": -2,
        "rowRange": 100, "lang": "zh",
    }
    try:
        with _client() as c:
            r = c.get(HKEX_SEARCH_URL, params=params)
            r.raise_for_status()
            rows = _parse_hkex_reports(r.json())
    except (httpx.HTTPError, ValueError) as e:
        raise FetchError(f"HKEXnews 公告检索失败：{_conn_hint(e)}")
    picked = []
    for it in rows:
        long_text = html.unescape(str(it.get("LONG_TEXT") or ""))
        title = html.unescape(str(it.get("TITLE") or ""))
        if "年報" not in long_text and "年报" not in long_text:
            continue
        if "中期" in long_text or "環境" in title or "ESG" in title.upper():
            continue
        m = re.search(r"(20\d{2})", title)
        y = int(m.group(1)) if m else 0
        if year and y != int(year):
            continue
        picked.append((y, it))
    if not picked:
        raise FetchError(f"HKEXnews 没有搜到「{company or code}」的年度报告")
    picked.sort(key=lambda x: (x[0], str(x[1].get("DATE_TIME") or "")), reverse=True)
    downloaded, skipped = [], []
    for y, it in picked[:max(1, int(limit or 1))]:
        title = html.unescape(str(it.get("TITLE") or "年度报告"))
        link = str(it.get("FILE_LINK") or "").strip()
        if not link:
            skipped.append({"title": title, "error": "没有附件地址"})
            continue
        url = link if link.startswith("http") else HKEX_PDF_BASE + link
        try:
            date = datetime.strptime(str(it.get("DATE_TIME") or ""), "%d/%m/%Y %H:%M").strftime("%Y%m%d")
        except ValueError:
            date = ""
        stem = sanitize(f"{company or stock.get('name') or code}_{y}年度报告" if y
                        else f"{company or stock.get('name') or code}_年度报告")[:60]
        try:
            dest = _download_pdf(url, stem)
        except FetchError as e:
            skipped.append({"title": title, "error": str(e)})
            continue
        downloaded.append({"title": title, "org": company or stock.get("name") or "",
                           "date": date, "file": dest.name, "url": url})
        time.sleep(_delay())
    if not downloaded:
        raise FetchError("港股年报下载失败：" + "；".join(f"{s['title']}（{s['error']}）" for s in skipped))
    return {"items": downloaded, "skipped": skipped, "url": HKEX_SEARCH_URL, "stock": stock}


def _hkex_pick(rows, kw_filter="", title="", title_any=None):
    """服务端宽查后的本地筛选：分类关键词、用户标题词、定期报告模式与无文件行的剔除。"""
    picked = []
    for it in rows:
        long_text = html.unescape(str(it.get("LONG_TEXT") or ""))
        item_title = html.unescape(str(it.get("TITLE") or ""))
        if kw_filter and kw_filter not in long_text and kw_filter not in item_title:
            continue
        if title and title not in long_text and title not in item_title:
            continue
        if title_any and not any(t in long_text or t in item_title for t in title_any):
            continue
        if title_any and any(x in long_text or x in item_title for x in ("摘要", "英文")):
            continue
        if not it.get("FILE_LINK"):
            continue
        picked.append(it)
    return picked


def fetch_hkex_announce(company, code="", category="annual", months=0, title="", limit=5,
                        title_any=None):
    category = _enum_or_raise(category or "annual", HKEX_CATEGORIES,
                              "港交所公告分类", hint=f"未知港交所公告分类：{category}")
    t1code, t2gcode, t2code, label, kw_filter = HKEX_CATEGORIES[category]
    stock = _hkex_stock(company, code)
    now = datetime.now()
    if int(months or 0) > 0:
        begin = now - timedelta(days=int(months) * 31)  # 月按 31 天折算，与东财口径一致
    else:
        begin = datetime(1993, 4, 1)  # 0 为不限，从披露易上线起查
    params = {
        "sortDir": 0, "sortByOptions": "DateTime", "category": 0, "market": "SEHK",
        "stockId": stock["stockId"], "documentType": -1,
        "fromDate": f"{begin:%Y%m%d}", "toDate": f"{now:%Y%m%d}",
        "title": str(title or ""), "searchType": 1,
        "t1code": t1code, "t2Gcode": t2gcode, "t2code": t2code,
        "rowRange": 100, "lang": "zh",
    }
    try:
        with _client() as c:
            r = c.get(HKEX_SEARCH_URL, params=params)
            r.raise_for_status()
            rows = _parse_hkex_reports(r.json())
    except (httpx.HTTPError, ValueError) as e:
        raise FetchError(f"HKEXnews 公告检索失败：{_conn_hint(e)}")
    picked = _hkex_pick(rows, kw_filter=kw_filter, title=str(title or ""), title_any=title_any)
    if not picked:
        raise FetchError(f"HKEXnews 没有搜到「{company or code}」的{label}")
    picked.sort(key=lambda x: str(x.get("DATE_TIME") or ""), reverse=True)
    limit = max(1, min(int(limit or 5), 10))
    downloaded, skipped = [], []
    for it in picked[:limit]:
        item_title = html.unescape(str(it.get("TITLE") or label))
        link = str(it.get("FILE_LINK") or "").strip()
        url = link if link.startswith("http") else HKEX_PDF_BASE + link
        try:
            date = datetime.strptime(str(it.get("DATE_TIME") or ""), "%d/%m/%Y %H:%M").strftime("%Y%m%d")
        except ValueError:
            date = ""
        stem = sanitize(f"{company or stock.get('name') or code}_{date}_{item_title}")[:60]
        try:
            dest = _download_pdf(url, stem)
        except FetchError as e:
            skipped.append({"title": item_title, "error": str(e)})
            continue
        downloaded.append({"title": item_title, "org": company or stock.get("name") or "",
                           "date": date, "category": label, "file": dest.name, "url": url})
        time.sleep(_delay())
    if not downloaded:
        raise FetchError("港股公告下载失败：" + "；".join(f"{s['title']}（{s['error']}）" for s in skipped))
    return {"items": downloaded, "skipped": skipped, "url": HKEX_SEARCH_URL, "stock": stock}


HKEX_PERIODIC = {
    "interim": ("interim", ("中期報告", "半年度報告", "中期报告", "半年度报告")),
    "quarterly": ("quarterly", ("季度報告", "季度报告")),
    "q1": ("quarterly", ("第一季度報告", "第一季度报告", "一季度報告", "一季度报告")),
    "q3": ("quarterly", ("第三季度報告", "第三季度报告", "三季度報告", "三季度报告")),
}


def fetch_hkex_periodic(company, code="", kind="interim", days=400, limit=1):
    kind = _enum_or_raise(str(kind or "interim").lower(), HKEX_PERIODIC,
                          "港股定期报告类型", hint=f"未知港股定期报告类型：{kind}")
    category, patterns = HKEX_PERIODIC[kind]
    # days 向上折成整月（400 天 → 14 个月），窗口只放大不缩小
    months = (int(days or 400) + 30) // 31
    return fetch_hkex_announce(company, code, category=category, months=months,
                               title_any=patterns, limit=limit)


def fetch_periodic_report(company, code="", market="", kind="interim", days=400, limit=1):
    """公司定期报告统一入口：A 股走巨潮，港股走 HKEXnews；年报沿用既有年报取数。"""
    company = str(company or "").strip()
    code = str(code or "").strip().upper()
    market = str(market or "").lower()
    if not market:
        market = "hk" if code.endswith(".HK") else ("a" if code else "")
    else:
        market = _enum_or_raise(market, ("a", "hk"), "market（a=A股，hk=港股）")
    kind = str(kind or "interim").lower()
    if kind == "annual":
        return fetch_annual_report(company, code, market, limit=limit)
    if market == "hk":
        return fetch_hkex_periodic(company, code, kind=kind, days=days, limit=limit)
    return fetch_cninfo_periodic(company, kind=kind, days=days, limit=limit, code=code)


def fetch_annual_report(company, code="", market="", year=None, limit=1):
    company = str(company or "").strip()
    code = str(code or "").strip().upper()
    market = str(market or "").lower()
    if not market:
        market = "hk" if code.endswith(".HK") else ("a" if code else "")
    else:
        market = _enum_or_raise(market, ("a", "hk"), "market（a=A股，hk=港股）")
    if market == "hk":
        return fetch_hkex_annual(company, code, year=year, limit=limit)
    try:
        return fetch_cninfo_annual(company, year=year, limit=limit)
    except FetchError as e:
        if code:
            raise
        try:
            return fetch_hkex_annual(company, code, year=year, limit=limit)
        except FetchError:
            raise e


# ---------- AKShare（conda macro-data 子进程） ----------


def _stamp_norm(value):
    """AKShare 时点归一：返回 (显示, 排序键)。解析不出键时显示保留原样、键为 None。

    帧首列有 年月（2026年09月份）、季度（2026年第1-3季度）、epoch 毫秒/秒、
    日期（2026-09-01 / 202609）四类写法；东财系序列还是降序排列，归一并算出
    排序键后才能安全取最近 N 期（tail 取旧行的教训见待修清单第 49 条）。
    本函数经 inspect.getsource 注入子进程脚本，改动只写这一处。"""
    import datetime as _dt
    import re as _re

    def _ym(y, m):
        y, m = int(y), int(m)
        return "%04d-%02d" % (y, m), y * 12 + m

    s = str(value).strip()
    m = _re.fullmatch(r"(20\d\d)年\s*(\d{1,2})月(份)?", s)
    if m:
        return _ym(m.group(1), m.group(2))
    m = _re.fullmatch(r"(20\d\d)[-\./年](\d{1,2})(?:[-\./月](\d{1,2})日?)?", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if m.group(3):
            return "%04d-%02d-%02d" % (y, mo, int(m.group(3))), y * 12 + mo
        return _ym(y, mo)
    m = _re.fullmatch(r"(20\d\d)(\d\d)\b", s)
    if m:
        return _ym(m.group(1), m.group(2))
    m = _re.fullmatch(r"(20\d\d)年第(\d)(?:-(\d))?季度", s)
    if m:
        y, q = int(m.group(1)), int(m.group(3) or m.group(2))
        return s, y * 4 + q
    if _re.fullmatch(r"\d{13}", s):
        t = _dt.datetime.fromtimestamp(int(s) / 1000, _dt.timezone.utc)
        return t.strftime("%Y-%m-%d"), t.year * 12 + t.month
    if _re.fullmatch(r"\d{10}", s):
        t = _dt.datetime.fromtimestamp(int(s), _dt.timezone.utc)
        return t.strftime("%Y-%m-%d"), t.year * 12 + t.month
    return s, None


_AKSHARE_TMPL = """
import contextlib, io, json, sys
import akshare as ak
try:
    import pandas as _pd
    if int(str(_pd.__version__).split(".")[0]) >= 3:
        # akshare 尚未适配 pandas 3（pandas 3 对字符串列赋整数从警告升级为报错）
        print("AKSHARE_ENV_INCOMPAT: akshare %s 未适配 pandas %s，"
              "宏观序列会报 TypeError。把 macro-data 环境的 pandas 降到 2.x 可解"
              % (ak.__version__, _pd.__version__))
        sys.exit(3)
except ImportError:
    pass
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    df = getattr(ak, __SERIES__)()
__STAMP_FN__
col0 = str(df.columns[0])
norm = [_stamp_norm(v) for v in df[col0]]
df = df.copy()
df[col0] = [n[0] for n in norm]
keys = [n[1] for n in norm]
ordered = bool(keys) and all(k is not None for k in keys)
if ordered:
    order = sorted(range(len(keys)), key=lambda i: keys[i])
    df = df.iloc[order].reset_index(drop=True)
df = df.tail(__ROWS__)
recs = json.loads(df.to_json(orient="records", force_ascii=False))
cols = [str(c) for c in df.columns]
print("<<<JSON>>>")
print(json.dumps({"columns": cols, "records": recs, "ordered": ordered}, ensure_ascii=False))
"""

AKSHARE_SCRIPT = _AKSHARE_TMPL.replace("__STAMP_FN__", inspect.getsource(_stamp_norm))


def macro_via_akshare(series, periods=24):
    series = str(series or "").strip()
    if series not in AKSHARE_SERIES:
        raise FetchError(f"不在允许的 AKShare 序列清单里：{series}")
    py = _cfg().get("macro_python") or ""
    if not py or not Path(py).exists():
        raise FetchError(f"找不到 macro-data 环境的 python（config.json 的 fetcher.macro_python: {py or '未配置'}）")
    periods = max(1, min(int(periods or 24), 120))
    code = (AKSHARE_SCRIPT
            .replace("__SERIES__", repr(series))
            .replace("__ROWS__", str(periods)))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=_timeout(), env=env)
    except subprocess.TimeoutExpired:
        raise FetchError(f"AKShare 取数超时（{_timeout():.0f} 秒），稍后重试")
    if proc.returncode == 3 and "AKSHARE_ENV_INCOMPAT" in proc.stdout:
        msg = proc.stdout.strip().rsplit("AKSHARE_ENV_INCOMPAT:", 1)[1].strip()
        raise FetchError(f"AKShare 环境不兼容：{msg}")
    if proc.returncode != 0:
        raise FetchError("AKShare 取数失败：" + (proc.stderr or proc.stdout or "未知错误").strip()[-300:])
    marker = "<<<JSON>>>"
    if marker not in proc.stdout:
        raise FetchError("AKShare 没有返回数据：" + proc.stdout.strip()[-200:])
    payload = json.loads(proc.stdout.split(marker, 1)[1].strip().splitlines()[0])
    return {"series": series, "columns": payload.get("columns") or [],
            "records": payload.get("records") or [],
            "ordered": bool(payload.get("ordered"))}


def _akshare_rows(payload):
    records = payload.get("records") or []
    if not records:
        raise FetchError("AKShare 返回空表，接口可能变了")
    first = str(payload["columns"][0])
    rows = []
    for rec in records:
        stamp = str(rec.get(first) or "")
        for key, val in rec.items():
            if key == first or val in (None, "", "nan", "None"):
                continue
            rows.append({"指标": f"{payload['series']}·{key}", "数值": str(val), "单位": "—",
                         "口径": AKSHARE_SERIES.get(payload["series"], payload["series"]),
                         "时点": stamp})
    if not rows:
        raise FetchError("AKShare 表里没有可用的数值列")
    return rows


def _akshare_stale_note(rows, months_limit=6):
    """最新期距今超过阈值时返回停更警示（无警示返回空串）。时点经 _stamp_norm 归一成
    YYYY-MM 或 YYYY-MM-DD 后才能比；归不出来的序列跳过警示。"""
    best_key, best_label = None, ""
    now = datetime.now()
    for r in rows:
        m = re.fullmatch(r"(20\d\d)-(\d{1,2})(?:-\d{1,2})?", str(r.get("时点") or "").strip())
        if not m:
            continue
        key = int(m.group(1)) * 12 + int(m.group(2))
        if best_key is None or key > best_key:
            best_key, best_label = key, str(r["时点"])
    if best_key is None:
        return ""
    lag = (now.year * 12 + now.month) - best_key
    if lag >= months_limit:
        return f"源最新期停在 {best_label}（滞后 {lag} 个月），疑似停更"
    return ""


# ---------- 乘联会 ----------

CPCA_PERIOD_LABELS = ["当月销量", "累计销量", "当月份额", "累计份额"]
CPCA_PERIOD_UNITS = ["万辆", "万辆", "%", "%"]
CPCA_SCOPES = {
    1: ["狭义乘用车", "广义乘用车"],
    3: None,
    4: ["国别"],
    5: None,
    6: ["整体市场", "新能源车型", "动力类型"],
}


def _cpca_stamp(text):
    """把 2026年9月 / 2026年 1-9月 / 2026-9月 统一成可读时点。"""
    text = re.sub(r"\s+", "", str(text or ""))
    m = re.fullmatch(r"(20\d{2})-(\d{1,2})月", text)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月"
    m = re.fullmatch(r"(20\d{2})年(\d{1,2})-(\d{1,2})月", text)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}-{int(m.group(3))}月"
    m = re.fullmatch(r"(20\d{2})年(\d{1,2})月", text)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月"
    return text


def _cpca_rows(data, charttype, months=12, metric="全部", top=15):
    months = max(1, min(int(months or 12), 36))
    top = max(1, min(int(top or 15), 60))
    metric = str(metric or "全部").strip()
    want = lambda m: metric in ("", "全部") or metric == m
    rows = []

    def is_num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    def has_num(vals):
        return isinstance(vals, list) and any(is_num(v) for v in vals)

    def add(scope, stamp, names, vals, units):
        prefix = f"{scope}·" if scope else ""
        for i, name in enumerate(names):
            if i < len(vals) and want(name) and is_num(vals[i]):
                rows.append({"指标": f"{prefix}{name}", "数值": vals[i], "单位": units[i],
                             "口径": CPCA_CHARTS[charttype], "时点": stamp})

    def add_yoy(scope, stamp, vals, names):
        prefix = f"{scope}·" if scope else ""
        if isinstance(vals, list) and has_num(vals):
            for i, name in enumerate(names):
                if i < len(vals) and want(name) and is_num(vals[i]):
                    rows.append({"指标": f"{prefix}{name}同比", "数值": vals[i], "单位": "%",
                                 "口径": CPCA_CHARTS[charttype], "时点": stamp})

    def recent_months(dl):
        """最近 N 个当年有数的月份；dataList 是从 1 月到 12 月排列，
        年末月份只有上年值，直接切尾会切到去年。"""
        years = set()
        for item in dl:
            for key, vals in item.items():
                m = re.fullmatch(r"(20\d{2})年", str(key))
                if m and has_num(vals):
                    years.add(m.group(1))
        if not years:
            return None, []
        latest = max(years)
        valid = [it for it in dl if has_num(it.get(f"{latest}年"))]

        def month_no(item):
            m = re.match(r"(\d{1,2})月", str(item.get("month") or ""))
            return int(m.group(1)) if m else 0

        valid.sort(key=month_no)
        return latest, valid[-months:]

    def recent_periods(dl):
        """月份键结构的块（4/5/6 的细分类）只取有数的月份。
        占比块的排列是 2026 年在前、2025 年在后，不能按列表位置切尾。"""
        def period_order(item):
            text = re.sub(r"\s+", "", str(item.get("月份") or ""))
            m = re.match(r"^(20\d{2})-(\d{1,2})月$", text)
            return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

        valid = [it for it in dl
                 if any(has_num(v) for k, v in it.items() if k != "月份")]
        valid.sort(key=period_order)
        return valid[-months:]

    for idx, block in enumerate(data):
        dl = block.get("dataList") or []
        if not dl:
            continue
        cat = str(block.get("category") or "").strip()
        if charttype == 2:
            scopes = ["狭义乘用车·累计", "狭义乘用车·当月", "广义乘用车·累计", "广义乘用车·当月"]
            scope = scopes[idx] if idx < len(scopes) else f"第{idx + 1}组"
            for item in dl[:top]:
                maker = str(item.get("厂商") or item.get("厂商名称") or "")
                maker_scope = f"{scope}·{maker}" if maker else scope
                for key, vals in item.items():
                    if key == "厂商" or not re.fullmatch(r"(20\d{2})年.+", str(key)) or not has_num(vals):
                        continue
                    add(maker_scope, _cpca_stamp(key), ["批发", "零售"], vals, ["万辆", "万辆"])
                period_key = max((k for k in item if re.fullmatch(r"(20\d{2})年.+", str(k))),
                                 default="")
                add_yoy(maker_scope, _cpca_stamp(period_key), item.get("同比"), ["批发", "零售"])
            continue

        scopes = CPCA_SCOPES.get(charttype)
        scope = scopes[idx] if scopes and idx < len(scopes) else (cat if cat else "")
        first = dl[0]
        if isinstance(first, dict) and "month" in first:
            latest, recent = recent_months(dl)
            for item in recent:
                month = re.sub(r"\s+", "", str(item.get("month") or ""))
                for key in sorted(k for k in item if re.fullmatch(r"(20\d{2})年", str(k))):
                    vals = item.get(key)
                    if not has_num(vals):
                        continue
                    year = re.fullmatch(r"(20\d{2})年", key).group(1)
                    add(scope, f"{year}年{month}" if month else f"{year}年",
                        ["产量", "批发", "零售", "出口"], vals, ["万辆"] * 4)
                if latest:
                    add_yoy(scope, f"{latest}年{month}" if month else f"{latest}年",
                            item.get("同比"), ["产量", "批发", "零售", "出口"])
        elif isinstance(first, dict) and "月份" in first:
            for item in recent_periods(dl):
                stamp = _cpca_stamp(item.get("月份"))
                for name, vals in item.items():
                    if name == "月份" or not has_num(vals):
                        continue
                    add(f"{scope}·{name}" if scope else str(name), stamp,
                        CPCA_PERIOD_LABELS, vals, CPCA_PERIOD_UNITS)

    if not rows:
        raise FetchError("乘联会接口返回空表，charttype 或字段结构可能变了")
    return rows


def fetch_cpca_sales(charttype=1, months=12, metric="全部", top=15):
    ct = _enum_or_raise(charttype or 1, CPCA_CHARTS, "乘联会 charttype（1-6）")
    metric = _enum_or_raise(metric or "全部",
                            ("全部", "产量", "批发", "零售", "出口") + tuple(CPCA_PERIOD_LABELS),
                            "乘联会指标")
    data = _get_json(CPCA_API_URL, params={"charttype": ct})
    if isinstance(data, dict):
        data = data.get("data") or []
    if not data:
        raise FetchError(f"乘联会 charttype={ct} 没有返回数据")
    rows = _cpca_rows(data, ct, months=months, metric=metric, top=top)
    return {"rows": rows, "charttype": ct, "label": CPCA_CHARTS[ct],
            "url": f"{CPCA_API_URL}?charttype={ct}"}


# ---------- 中证指数 ----------

def _csindex_get(path, params=None):
    data = _get_json(f"{CSINDEX_BASE}{path}", params=params)
    if not isinstance(data, dict) or str(data.get("code")) != "200":
        msg = data.get("msg") if isinstance(data, dict) else data
        raise FetchError(f"中证指数接口 {path} 返回异常：{msg}")
    return data.get("data")


def fetch_csindex_snapshot(code):
    code = re.sub(r"\D", "", str(code or ""))
    if len(code) != 6:
        raise FetchError("中证指数需要 6 位指数代码，如 000300、930997")
    basic = _csindex_get(f"/indexInfo/index-basic-info/{code}") or {}
    if not basic:
        raise FetchError(f"中证指数没有指数代码「{code}」")
    name = str(basic.get("indexShortNameCn") or basic.get("indexFullNameCn") or code)
    rows = []

    def add(indicator, value, unit="", caliber="", stamp=""):
        if value in (None, "", "None", "-"):
            return
        rows.append({"指标": indicator, "数值": value, "单位": unit,
                     "口径": caliber or name, "时点": stamp})

    add("指数简称", name)
    add("指数全称", basic.get("indexFullNameCn"))
    add("基日", basic.get("basicDate"))
    add("基点", basic.get("basicIndex"))
    add("发布日", basic.get("publishDate"))
    try:
        feature = _csindex_get(f"/indexInfo/index-feature/{code}") or {}
        add("成分数量", feature.get("consNum"), "只", "最新成分", str(feature.get("tradeDate") or ""))
    except FetchError:
        feature = {}
    try:
        perf = _csindex_get("/data-service/indexPerformance", params={"indexCode": code}) or {}
        hit = next((x for x in perf.get("mainIndexPerfList") or []
                    if str(x.get("indexCode") or "") == code), None)
        if hit:
            stamp = str(hit.get("tradeDate") or "")
            add("最新收盘", hit.get("closeIndex"), "点", name, stamp)
            add("当日涨跌幅", hit.get("changePct"), "%", name, stamp)
            add("年内涨跌幅", hit.get("changePctThisYear"), "%", name, stamp)
    except FetchError:
        pass
    try:
        val = _csindex_get("/data-service/indexValuation", params={"indexCode": code}) or {}
        hit = next((x for x in val.get("indexValuations") or []
                    if str(x.get("indexName") or "") == name), None)
        if hit:
            stamp = str(val.get("tradeDate") or "")
            add("市盈率 PE", hit.get("pe"), "倍", name, stamp)
            add("市净率 PB", hit.get("pb"), "倍", name, stamp)
            add("股息率", hit.get("dp"), "%", name, stamp)
    except FetchError:
        pass
    try:
        y = _csindex_get(f"/perf/get-index-yield-item/{code}") or {}
        stamp = str(y.get("endDate") or "")
        for key, label in [("oneMonth", "近1月涨跌幅"), ("threeMonth", "近3月涨跌幅"),
                           ("thisYear", "今年以来涨跌幅"), ("oneYear", "近1年涨跌幅"),
                           ("threeYear", "近3年涨跌幅"), ("fiveYear", "近5年涨跌幅")]:
            add(label, y.get(key), "%", name, stamp)
    except FetchError:
        pass
    try:
        top = _csindex_get(f"/index/weight/top10new/{code}") or {}
        stamp = str(top.get("updateDate") or "")
        add("前五大权重合计", top.get("top5Sum"), "%", name, stamp)
        add("前十大权重合计", top.get("top10Sum"), "%", name, stamp)
        for it in top.get("weightList") or []:
            add(f"前十大权重·{it.get('securityName')}", it.get("weight"), "%",
                f"{name}·{it.get('cics1stName') or ''}", stamp)
    except FetchError:
        pass
    try:
        ind = _csindex_get(f"/index/weight/industry-weight-two-new/{code}", params={"cicsType": 1}) or {}
        stamp = str(ind.get("updateDate") or "")
        for it in ind.get("industryWeightList") or []:
            add(f"行业权重·{it.get('csiType')}", it.get("weightPct"), "%", name, stamp)
    except FetchError:
        pass
    if not rows:
        raise FetchError(f"中证指数「{code}」没有取到任何字段")
    return {"rows": rows, "name": name, "code": code,
            "url": f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={code}"}




# ---------- 人民银行统计 ----------

def _pbc_links(page_url):
    try:
        with _client({"Referer": "http://www.pbc.gov.cn/"}) as c:
            r = c.get(page_url)
            r.raise_for_status()
            text = _decode(r)
    except httpx.HTTPError as e:
        raise FetchError(f"人行统计页打不开：{_conn_hint(e)}")
    links = []
    for href in re.findall(r'href="([^"]+\.(?:xlsx|xls))"', text, re.I):
        full = urljoin(page_url, href)
        if full not in links:
            links.append(full)
    return links


def _pbc_excel(content):
    import pandas as pd
    try:
        book = pd.ExcelFile(io.BytesIO(content))
    except Exception as e:
        raise FetchError(f"人行附件不是可读的 Excel：{e}")
    for sheet in book.sheet_names:
        df = pd.read_excel(io.BytesIO(content), sheet_name=sheet, header=None)
        if df.dropna(how="all").empty:
            continue
        title = ""
        for value in df.iloc[:3].values.flatten():
            text = str(value).strip()
            if text and text.lower() != "nan":
                title = text
                break
        return title, df, sheet
    raise FetchError("人行 Excel 里没有非空工作表")


def _pbc_rows_from_df(df, title, months=12):
    def cell(v):
        raw = "" if v is None else str(v)
        if raw.strip().lower() in ("nan", "none", "nat"):
            raw = ""
        return re.sub(r"\s+", " ", raw).strip()

    header_idx, date_cols = None, {}
    for i in range(min(20, len(df))):
        row = [cell(v) for v in df.iloc[i].tolist()]
        found = {}
        for j, text in enumerate(row):
            m = re.match(r"^(20\d{2})[.\-/年](\d{1,2})", text)
            if m:
                found[j] = f"{m.group(1)}-{m.group(2).zfill(2)}"
        if found and (any("项目" in x or "Item" in x for x in row) or len(found) >= 2):
            header_idx, date_cols = i, found
            break
    if header_idx is None or not date_cols:
        raise FetchError(f"人行表「{title}」没找到「项目 + 月份」表头")

    # 同一个月可能跨两列（官方储备资产：亿美元 / 亿SDR），按日期列切分列区间
    positions = sorted(date_cols)
    spans = {}
    for k, j in enumerate(positions):
        end = positions[k + 1] if k + 1 < len(positions) else len(df.columns)
        spans[date_cols[j]] = list(range(j, end))

    # 单位行：货币当局表写在表头上方（单位：亿元人民币），官方储备资产写在下方（亿美元/亿SDR）
    default_unit = ""
    unit_rows = list(range(max(0, header_idx - 4), header_idx)) + \
        list(range(header_idx + 1, min(header_idx + 4, len(df))))
    for i in unit_rows:
        for text in (cell(v) for v in df.iloc[i].tolist()):
            m = re.match(r"^单位[:：]\s*(.+)$", text)
            if m:
                default_unit = m.group(1).strip()
                break
        if default_unit:
            break
    units = {}
    if not default_unit:
        for i in unit_rows:
            row = [cell(v) for v in df.iloc[i].tolist()]
            found = {}
            for span in spans.values():
                for j in span:
                    if j < len(row) and row[j] and not re.match(r"^[\d.,]+$", row[j]):
                        found[j] = row[j]
            if len(found) >= len(spans):
                units = found
                break

    # 末尾常挂着尚未填数的未来月份，按实际有数的月份取最近 N 期
    valid_stamps = set()
    for i in range(header_idx + 1, len(df)):
        label = cell(df.iat[i, 0])
        if not label or not re.search(r"[一-鿿]", label):
            continue
        for stamp, span in spans.items():
            for j in span:
                if j < len(df.columns) and re.match(
                        r"^-?\d+(?:\.\d+)?$", cell(df.iat[i, j]).replace(",", "")):
                    valid_stamps.add(stamp)
                    break
    keep = sorted(valid_stamps)[-max(1, min(int(months or 12), 24)):]
    rows = []
    for i in range(header_idx + 1, len(df)):
        label = cell(df.iat[i, 0])
        if not label or not re.search(r"[一-鿿]", label) or label.startswith(("注", "说明")):
            continue
        for stamp, span in spans.items():
            if stamp not in keep:
                continue
            for j in span:
                if j >= len(df.columns):
                    continue
                text = cell(df.iat[i, j]).replace(",", "")
                m = re.match(r"^(-?\d+(?:\.\d+)?)\s*(.*)$", text)
                if not m:
                    continue
                unit = m.group(2).strip() or units.get(j) or default_unit or "—"
                rows.append({"指标": label, "数值": m.group(1), "单位": unit,
                             "口径": title, "时点": stamp})
    return rows


def fetch_pbc_stat(dataset="货币统计概览", keyword="", months=12):
    dataset = _enum_or_raise(dataset or "货币统计概览", PBC_PAGES, "人行统计数据集")
    links = _pbc_links(PBC_PAGES[dataset])
    if not links:
        raise FetchError(f"人行「{dataset}」页面没有挂 Excel 附件")
    keyword = str(keyword or "").strip()
    found_titles = []
    matched = []
    for link in links[:20]:
        try:
            with _client({"Referer": PBC_PAGES[dataset]}) as c:
                r = c.get(link)
                r.raise_for_status()
                content = r.content
        except httpx.HTTPError as e:
            found_titles.append(f"{link.rsplit('/', 1)[-1]}（{_conn_hint(e)}）")
            continue
        try:
            title, df, _ = _pbc_excel(content)
        except FetchError as e:
            found_titles.append(str(e))
            continue
        found_titles.append(title)
        if keyword and keyword not in title:
            continue
        matched.append(title)
        rows = _pbc_rows_from_df(df, title, months=months)
        if not rows:
            continue
        return {"rows": rows, "title": title, "dataset": dataset,
                "url": link, "page": PBC_PAGES[dataset]}
    tail = "；".join(found_titles[:6])
    if matched:
        raise FetchError(f"人行「{dataset}」的表「{matched[0]}」解析不出数据；看到的有：{tail}")
    if keyword:
        raise FetchError(f"人行「{dataset}」最近附件里没有标题含「{keyword}」的表；看到的有：{tail}")
    raise FetchError(f"人行「{dataset}」附件解析不出数据；看到的有：{tail}")


# ---------- 国际宏观（FRED / IMF / DBnomics / World Bank） ----------

def fetch_fred_series(series_id, periods=24):
    series_id = str(series_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,60}", series_id):
        raise FetchError("FRED 序列号只能是字母、数字、下划线和点，如 GDPC1、CPIAUCSL")
    try:
        with _client() as c:
            r = c.get(FRED_CSV_URL, params={"id": series_id})
            r.raise_for_status()
            text = _decode(r)
    except httpx.HTTPError as e:
        raise FetchError(f"FRED 接口请求失败：{_conn_hint(e)}")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    rows = []
    for line in reader:
        if len(line) < 2 or not line[1] or line[1] == ".":
            continue
        rows.append({"指标": series_id, "数值": line[1], "单位": "",
                     "口径": f"FRED 序列 {series_id}", "时点": line[0]})
    rows = rows[-max(1, min(int(periods or 24), 120)):]
    if not rows:
        raise FetchError(f"FRED 序列「{series_id}」没有观测值（{header[:2]}）")
    return {"rows": rows, "series": series_id,
            "url": f"{FRED_CSV_URL}?id={series_id}"}


def fetch_imf_series(indicator, countries="CHN", periods=24):
    indicator = str(indicator or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,60}", indicator):
        raise FetchError("IMF 指标代码只能用字母、数字、下划线和点，如 NGDP_RPCH")
    countries = str(countries or "CHN").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_,]{3,120}", countries):
        raise FetchError("IMF 国家代码用逗号分隔，如 CHN,USA,IND")
    data = _get_json(f"{IMF_API_BASE}/{quote(indicator)}/{quote(countries)}")
    values = (data.get("values") or {}).get(indicator) or {}
    wanted = [c.strip() for c in countries.split(",") if c.strip()]
    # DataMapper 的路径国家参数不生效，实际返回全部经济体，必须自己过滤
    missing = [c for c in wanted if c not in values]
    if missing and len(missing) == len(wanted):
        raise FetchError(f"IMF 指标「{indicator}」没有国家代码：{'、'.join(missing)}")
    rows = []
    for country in wanted:
        points = sorted((str(y), v) for y, v in (values.get(country) or {}).items() if v is not None)
        for year, value in points[-max(1, min(int(periods or 24), 120)):]:
            rows.append({"指标": f"{indicator}·{country}", "数值": value, "单位": "",
                         "口径": f"IMF DataMapper {indicator}", "时点": year})
    if not rows:
        raise FetchError(f"IMF 指标「{indicator}」在「{countries}」没有数据")
    return {"rows": rows, "indicator": indicator, "countries": countries,
            "url": f"{IMF_API_BASE}/{indicator}/{countries}"}


def fetch_dbnomics_series(series, periods=24):
    series = str(series or "").strip().strip("/")
    parts = series.split("/")
    if len(parts) != 3 or not all(parts):
        raise FetchError("DBnomics 序列写成 provider/dataset/series，如 IMF/WEO:2024-10/CHN.NGDP_RPCH")
    url = "/".join([DBNOMICS_API_BASE] + [quote(p, safe="+") for p in parts])
    data = _get_json(url, params={"observations": "1"})
    docs = ((data.get("series") or {}).get("docs") or [])
    rows = []
    for doc in docs:
        code = str(doc.get("series_code") or doc.get("series_name") or series)
        periods_list = doc.get("period") or []
        values = doc.get("value") or []
        points = [(str(p), v) for p, v in zip(periods_list, values) if v is not None]
        for stamp, value in points[-max(1, min(int(periods or 24), 120)):]:
            rows.append({"指标": code, "数值": value, "单位": "",
                         "口径": f"DBnomics {series}", "时点": stamp})
    if not rows:
        raise FetchError(f"DBnomics 序列「{series}」没有观测值")
    return {"rows": rows, "series": series, "url": url}


def fetch_worldbank_series(indicator, countries="CHN", periods=24):
    indicator = str(indicator or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,80}", indicator):
        raise FetchError("World Bank 指标代码示例：NY.GDP.MKTP.CD")
    countries = str(countries or "CHN").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_;]{2,120}", countries):
        raise FetchError("World Bank 国家代码用分号分隔，如 CHN;USA")
    data = _get_json(f"{WORLDBANK_API_BASE}/country/{countries}/indicator/{indicator}",
                     params={"format": "json", "per_page": 1000})
    if not isinstance(data, list) or len(data) < 2 or not data[1]:
        raise FetchError(f"World Bank 指标「{indicator}」没有数据")
    grouped = {}
    for item in data[1]:
        if item.get("value") is None:
            continue
        country = str((item.get("country") or {}).get("value") or item.get("countryiso3code") or "")
        grouped.setdefault(country, []).append((str(item.get("date") or ""), item["value"]))
    rows = []
    for country, points in grouped.items():
        for stamp, value in sorted(points)[-max(1, min(int(periods or 24), 120)):]:
            rows.append({"指标": f"{indicator}·{country}", "数值": value, "单位": "",
                         "口径": f"World Bank {indicator}", "时点": stamp})
    if not rows:
        raise FetchError(f"World Bank 指标「{indicator}」在「{countries}」没有数据")
    return {"rows": rows, "indicator": indicator, "countries": countries,
            "url": f"{WORLDBANK_API_BASE}/country/{countries}/indicator/{indicator}"}


# ---------- 中汽协文字稿 ----------

# 中汽协的产销/数据栏目正文全是图片，取不到数字；这里只保留有正文的行业要闻与协会动态
CAAM_CATES = {"行业要闻": "cate_2", "协会动态": "cate_3"}


def fetch_caam_article(keyword, category="行业要闻", limit=1):
    category = _enum_or_raise(category or "行业要闻", CAAM_CATES, "中汽协栏目")
    list_url = f"{CAAM_BASE}/chn/1/{CAAM_CATES[category]}/list_1.html"
    try:
        with _client({"Referer": CAAM_BASE}) as c:
            r = c.get(list_url)
            r.raise_for_status()
            text = _decode(r)
    except httpx.HTTPError as e:
        raise FetchError(f"中汽协列表页打不开：{_conn_hint(e)}")
    # 只在正文列表区取链接，页脚“主要单位”“最新新闻”里也有 con_ 链接
    block = re.search(r'<div class="opinion">(.*?)<!-- 分页 -->', text, re.S)
    seg = block.group(1) if block else text
    items = []
    for href, title, date in re.findall(
            r'<a href="(con_\d+\.html)"[^>]*>.*?<span class="cont">(.*?)</span>'
            r'\s*<span class="time">\[(.*?)\]</span>', seg, re.S):
        title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", title))).strip()
        if not title:
            continue
        items.append({"title": title, "date": date.strip(),
                      "url": urljoin(list_url, href)})
    keyword = str(keyword or "").strip()
    picked = [x for x in items if keyword in x["title"]] if keyword else items
    if not picked:
        raise FetchError(f"中汽协「{category}」栏目没有标题含「{keyword}」的文章"
                         f"（最近有：{'、'.join(x['title'] for x in items[:5]) or '无'}）")
    limit = max(1, min(int(limit or 1), 5))
    results = []
    for it in picked[:limit]:
        try:
            with _client({"Referer": list_url}) as c:
                r = c.get(it["url"])
                r.raise_for_status()
                page = _decode(r)
        except httpx.HTTPError as e:
            raise FetchError(f"中汽协文章打不开：{_conn_hint(e)}")
        body = re.search(r'<div class="(?:form-)?fourBox">(.*?)</div>', page, re.S)
        seg = re.sub(r"<script.*?</script>", "", body.group(1) if body else "", flags=re.S)
        paragraphs = []
        for p in re.findall(r"<p[^>]*>(.*?)</p>", seg, re.S):
            plain = html.unescape(re.sub(r"<[^>]+>", "", p))
            plain = re.sub(r"\s+", " ", plain).strip()
            if len(plain) >= 15:
                paragraphs.append(plain)
        if len(" ".join(paragraphs)) < 60:
            raise FetchError(f"中汽协「{it['title']}」正文是图片，取不到文字；"
                             "产销数据请改用乘联会，行业文字稿可换一篇行业要闻")
        results.append({**it, "paragraphs": paragraphs})
    return {"items": results, "category": category, "url": list_url}


# ---------- 快照与入库 ----------

def make_snapshot(org, topic, url, rows, note=""):
    date = config.today_str().replace("-", "")
    stem = sanitize(f"{org}_{topic}快照_{date}") or f"取数快照_{date}"
    name = unique_target(config.SNAPSHOT_DIR, stem + ".docx")
    out = config.SNAPSHOT_DIR / name
    snapshot.build({
        "out": out.relative_to(config.ROOT).as_posix(),
        "title": f"{topic}快照（{org}）",
        "org": org, "url": url, "fetched_at": config.today_str(),
        "note": note or "本快照由网页端取数任务生成，数字以取数时点为准，跨期不可直接比较。",
        "rows": rows,
    })
    dup = find_duplicate(out)
    if dup is not None:
        out.unlink(missing_ok=True)
        return dup
    return out


def make_text_snapshot(org, topic, url, paragraphs, note=""):
    date = config.today_str().replace("-", "")
    stem = sanitize(f"{org}_{topic}摘录_{date}") or f"取数摘录_{date}"
    name = unique_target(config.SNAPSHOT_DIR, stem + ".docx")
    out = config.SNAPSHOT_DIR / name
    snapshot.build({
        "out": out.relative_to(config.ROOT).as_posix(),
        "title": f"{topic}摘录（{org}）",
        "org": org, "url": url, "fetched_at": config.today_str(),
        "note": note or "本摘录由网页端取数任务生成，只保留网页正文段落。",
        "paragraphs": paragraphs,
    })
    dup = find_duplicate(out)
    if dup is not None:
        out.unlink(missing_ok=True)
        return dup
    return out


def _snapshot_result(org, topic, url, rows, note, tid=None, targets=None):
    tasks.update(tid, detail=f"{topic}落快照")
    snap = make_snapshot(org, topic, url, rows, note=note)
    r = ingest_snapshot(snap, tid, targets=targets)
    summary = f"{topic} 取到 {len(rows)} 行，快照已落 {snap.name}"
    if r:
        summary += f"，入库 {r.get('entries_count', 0)} 条"
    return {"summary": summary, "rows": rows, "snapshot": snap.name,
            "files": [snap.relative_to(config.ROOT).as_posix()],
            "entries_count": int((r or {}).get("entries_count") or 0),
            "ingest_failed": [] if r and "error" not in r else [snap.name]}


def _garbled_ratio(text):
    """替换字符（U+FFFD）占比，用来判断这一段是不是解码失败的乱码。"""
    s = str(text or "")
    return s.count("�") / len(s) if s else 0.0


def _text_snapshot_result(org, topic, url, paragraphs, tid=None, targets=None, note=""):
    tasks.update(tid, detail=f"{topic}落摘录")
    # 乱码段写进快照后会被审校当成锚点原文，模型核不出数字只能瞎改；
    # 解码救不回来的段落直接剔除，并在快照说明里记一笔
    kept = [p for p in (paragraphs or [])
            if len(p) < 40 or _garbled_ratio(p) <= 0.05]
    if len(kept) < len(paragraphs or []):
        note = (note + "；" if note else "") + f"剔除 {len(paragraphs) - len(kept)} 段乱码"
        paragraphs = kept
    snap = make_text_snapshot(org, topic, url, paragraphs, note=note)
    r = ingest_snapshot(snap, tid, targets=targets)
    summary = f"{topic} 摘录 {len(paragraphs)} 段，快照已落 {snap.name}"
    if r:
        summary += f"，入库 {r.get('entries_count', 0)} 条"
    return {"summary": summary, "paragraphs": paragraphs, "snapshot": snap.name,
            "files": [snap.relative_to(config.ROOT).as_posix()],
            "entries_count": int((r or {}).get("entries_count") or 0),
            "ingest_failed": [] if r and "error" not in r else [snap.name]}


def ingest_snapshot(path, tid=None, targets=None):
    tasks.update(tid, detail=f"摄入快照 {path.name}")
    with tasks.kind_lock("ingest"):
        manifest = ingest_mod.load_manifest()
        before = {f["sha256"] for f in manifest["files"]}
        r = ingest_mod.ingest_one(path, manifest, targets=targets)
        ingest_mod.append_manifest([f for f in manifest["files"] if f["sha256"] not in before])
        ingest_mod.finish(ingest_mod.load_manifest(), [r] if r and "error" not in r else [])
    return r


def ingest_inbox(file_names, tid=None, targets=None, force_kind=None):
    names = [Path(n).name for n in file_names]
    classify_line, ingest_line = build_lines(names, targets=targets, force_kind=force_kind)
    with tasks.kind_lock("classify"):
        classified = classify_line(tid)
    with tasks.kind_lock("ingest"):
        ingested = ingest_line(tid)
    return {"classify": classified, "ingest": ingested}


# ---------- 网络检索兜底（专用源取不到时） ----------

BING_SEARCH_URL = "https://cn.bing.com/search"
SOGOU_SEARCH_URL = "https://www.sogou.com/web"
WEB_SOURCES_PATH = config.DATA_DIR / "web_sources.json"
LESSONS_PATH = config.DATA_DIR / "fetch_lessons.json"

# 字典、诗词、笔画类站点专吃中文长查询，正文没有一条产业数据，检索命中了也不能用
_JUNK_DOMAINS = ("zdic.net", "hanyu.baidu.com", "dict.baidu.com", "dict.youdao.com",
                 "cidian", "zidian", "chazidian", "guoxuedashi", "hydcd", "bihua",
                 "shici", "gushici", "xiehouyu", "ciku", "wenxue")
_DATA_HINT = re.compile(
    r"数据|统计|规模|销量|产量|产能|出口|进口|交付|上险|增速|复合|CAGR|占比|份额|市占|"
    r"价格|均价|营收|收入|利润|毛利|净利|保有量|装机|渗透率|金额|数量|排名|多少")
_STOP_KEYS = {"数据", "统计", "情况", "分析", "报告", "最新", "多少", "是什么",
              "怎么样", "中国", "行业", "市场"}


def known_web_sources():
    try:
        data = json.loads(WEB_SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("sources") if isinstance(data, dict) else []
    return [s for s in items or [] if isinstance(s, dict) and s.get("url")]


def _remember_web_sources(items):
    """把检索到并抓取成功的来源记进 data/web_sources.json，下次检索可优先复用。"""
    sources = known_web_sources()
    known = {s["url"] for s in sources}
    for it in items:
        if it.get("url") and it["url"] not in known and not _junk_url(it["url"]):
            sources.append({"title": it.get("title") or "", "url": it["url"],
                            "added": config.today_str()})
            known.add(it["url"])
    WEB_SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEB_SOURCES_PATH.write_text(json.dumps({"sources": sources}, ensure_ascii=False, indent=2),
                                encoding="utf-8")


def _load_lessons():
    try:
        data = json.loads(LESSONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("lessons") if isinstance(data, dict) else []
    return [x for x in items or [] if isinstance(x, dict)]


def _query_keys(query):
    keys = [k.strip().casefold() for k in re.split(r"[\s，,、；;：:/（）()]+", str(query or ""))
            if len(k.strip()) >= 2]
    # 年份不算关键词：只有「2026」命中的统计公报页与检索主题无关
    return [k for k in dict.fromkeys(keys)
            if k not in _STOP_KEYS and not re.fullmatch(r"(?:19|20)\d{2}", k)]


def _lesson_sites(query):
    """同一类检索以前成功过的站点，下次把它排进检索式，省得再撞字典页。"""
    keys = set(_query_keys(query))
    if not keys:
        return []
    scored = []
    for rec in _load_lessons():
        if not rec.get("ok"):
            continue
        overlap = len(keys & set(rec.get("keys") or []))
        if overlap >= 2:
            scored.append((overlap, rec))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for _, rec in scored:
        for d in rec.get("domains") or []:
            if d and d not in out:
                out.append(d)
    return out[:2]


def _remember_lesson(query, ok, domains, note=""):
    """把「检索词—站点—是否取到数据」记进 data/fetch_lessons.json，供下次检索复用。"""
    lessons = _load_lessons()
    lessons.append({"query": str(query or ""), "keys": _query_keys(query), "ok": bool(ok),
                    "domains": list(dict.fromkeys(d for d in domains if d)),
                    "note": str(note or ""), "date": config.today_str()})
    lessons = lessons[-300:]
    LESSONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LESSONS_PATH.write_text(json.dumps({"lessons": lessons}, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def _query_variants(query):
    """长中文检索式先拆短，再补数据词和已验证站点，避免整句被搜索引擎当成词典查询。"""
    base = re.sub(r"\s+", " ", str(query or "")).strip()
    if not base:
        return []

    def add(seq, x):
        x = re.sub(r"\s+", " ", x).strip()
        if x and x not in seq:
            seq.append(x)

    out = [base]
    add(out, re.sub(r"[（(][^）)]*[）)]", " ", base))
    add(out, re.sub(r"[、，,；;：:/]+", " ", base))
    if not _DATA_HINT.search(base):
        add(out, base + " 数据")
    stripped = re.sub(r"[、，,；;：:/]+", " ", base)
    for d in _lesson_sites(base):
        add(out, f"{stripped} site:{d}")
    return out[:4]


def _domain(url):
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _junk_url(url):
    u = str(url or "").lower()
    return any(d in u for d in _JUNK_DOMAINS)


def _clean_web_text(text):
    text = str(text or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    return re.sub(r"\s+", " ", text.replace("​", "").replace("﻿", "")).strip()


def _parse_bing(page, limit):
    items = []
    for block in re.findall(r'(?is)<li class="b_algo".*?</li>', page):
        m = re.search(r'(?is)<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        url = html.unescape(m.group(1))
        title = re.sub(r"(?s)<[^>]+>", "", html.unescape(m.group(2))).strip()
        sn = re.search(r"(?is)<p[^>]*>(.*?)</p>", block)
        snippet = re.sub(r"(?s)<[^>]+>", "", html.unescape(sn.group(1))).strip() if sn else ""
        if not url.startswith("http") or "bing.com" in url or "microsoft.com" in url:
            continue
        if _junk_url(url):
            continue
        items.append({"title": _clean_web_text(title), "url": url,
                      "snippet": _clean_web_text(snippet)})
        if len(items) >= limit:
            break
    return items


def _strip_tags(fragment):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return _clean_web_text(html.unescape(text))


def _sogou_target(href):
    """搜狗结果链接多是 /link?url=... 中转页，真正的网址写在跳转脚本里。"""
    href = html.unescape(str(href or ""))
    if href.startswith("http"):
        return href
    if not href.startswith("/link"):
        return ""
    try:
        with httpx.Client(timeout=12, headers={"User-Agent": UA}, follow_redirects=False) as c:
            r = c.get("https://www.sogou.com" + href)
    except httpx.HTTPError:
        return ""
    m = re.search(r'location\.replace\("([^"]+)"', r.text)
    return html.unescape(m.group(1)) if m else ""


def _parse_sogou(page, limit):
    items, seen = [], set()
    blocks = re.split(r'(?=<div class="vrwrap")', page)[1:]
    for block in blocks:
        m = re.search(r'(?is)<h3[^>]*class="[^"]*vr-title[^"]*"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                      block)
        if not m:
            continue
        title = _strip_tags(m.group(2))
        if not title:
            continue
        text = _strip_tags(block)
        text = re.split(r"推荐您搜索|大家还在搜|相关搜索", text)[0]
        snippet = text[len(title):].strip() if text.startswith(title) else text
        url = _sogou_target(m.group(1))
        if not url or "sogou.com" in url or _junk_url(url) or url in seen:
            continue
        seen.add(url)
        items.append({"title": title, "url": url, "snippet": snippet[:300]})
        if len(items) >= limit:
            break
    return items


def _relevant(item, query):
    """必应退化成年度词条时，用非年份关键词卡住：标题和摘要至少命中两个。"""
    keys = [k for k in _query_keys(query) if not re.fullmatch(r"(?:19|20)\d{2}", k)]
    if not keys:
        return True
    text = f"{item.get('title') or ''} {item.get('snippet') or ''}".casefold()
    return sum(1 for k in keys if k in text) >= min(2, len(keys))


def fetch_web_search(query, limit=5):
    query = str(query or "").strip()
    if not query:
        raise FetchError("网络检索需要关键词")
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    variants = _query_variants(query)
    items, seen = [], set()
    errors = []
    # 先把搜狗的所有变体试完，再退必应；避免一个变体临时失败就把整次检索交给降级的必应
    for engine in ("sogou", "bing"):
        for q in variants:
            got = []
            try:
                with httpx.Client(timeout=20, headers=headers, follow_redirects=True) as c:
                    if engine == "sogou":
                        r = c.get(SOGOU_SEARCH_URL, params={"query": q})
                        r.raise_for_status()
                        got = _parse_sogou(r.text, limit * 2)
                    else:
                        r = c.get(BING_SEARCH_URL, params={"q": q, "count": max(10, limit * 2),
                                                           "setlang": "zh-CN"})
                        r.raise_for_status()
                        got = [it for it in _parse_bing(r.text, limit * 2)
                               if _relevant(it, query)]
            except httpx.HTTPError as e:
                errors.append(f"{engine} {e}")
            for it in got:
                if it["url"] not in seen:
                    seen.add(it["url"])
                    items.append(it)
            if len(items) >= max(limit * 2, 6):
                break
        if items:
            break
    if not items:
        raise FetchError(f"检索「{query}」没有结果" + (f"（{'；'.join(errors[-2:])}）" if errors else ""))
    return {"query": query, "items": items[:max(limit * 2, 6)]}


_PAGE_JUNK_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside",
                   "form", "svg", "button", "iframe", "template"}
_PAGE_JUNK_ATTR = re.compile(
    r"(?:^|[-_ ])(nav|menu|footer|header|sidebar|breadcrumb|copyright|advert|ads?|"
    r"recommend|related|comment|share|toolbar|search|login|subscribe)(?:[-_ ]|$)", re.I)
_PAGE_CONTENT_ATTR = re.compile(r"article|content|post|rich_media|detail|main|text", re.I)
_PAGE_DATE_FIELD_RE = re.compile(
    r'(?:publishTime|publish_time|pubdate|publishdate|datepublished|datePublished|'
    r'article:published_time|og:published_time)[^0-9]{0,40}'
    r'(20\d{2})-(\d{1,2})-(\d{1,2})', re.I)
_PAGE_FULL_DATE_RE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日")
_PAGE_NUM_DATE_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
_PAGE_TITLE_YM_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")
_PAGE_Q_RE = re.compile(r"(?<![\d])(2\d)\s*[Qq]\s*([1-4])")


class _PageTextParser(HTMLParser):
    """只收正文容器里的段落，丢掉导航、页脚、推荐位与脚本。

    有 article/main 或 class/id 像正文的容器时只用容器内文字；整页都没有容器才退回收全部
    非噪声段落。返回的段落按原顺序拼接，供关键词窗口选取。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []   # [(tag, junk, content)]
        self.blocks = []  # [(text, in_content)]
        self.buf = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = dict(attrs)
        ident = f"{attr.get('class') or ''} {attr.get('id') or ''}"
        junk = tag in _PAGE_JUNK_TAGS or bool(_PAGE_JUNK_ATTR.search(ident))
        content = tag in ("article", "main") or bool(_PAGE_CONTENT_ATTR.search(ident))
        self.stack.append((tag, junk, content))
        if tag in ("p", "br", "div", "li", "section", "tr", "h1", "h2", "h3", "h4"):
            self._flush()

    def handle_endtag(self, tag):
        self._flush()
        tag = tag.lower()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if any(junk for _t, junk, _c in self.stack):
            return
        self.buf.append(data)

    def _flush(self):
        text = _clean_web_text("".join(self.buf))
        if text:
            self.blocks.append((text, any(c for _t, _j, c in self.stack)))
        self.buf = []

    def text(self):
        self._flush()
        content = [t for t, c in self.blocks if c]
        chosen = content if content else [t for t, _c in self.blocks]
        return " ".join(chosen)


def _title_year(title):
    """标题里最晚提到的年份；没有返回 None。"""
    years = [int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(title or ""))]
    return max(years) if years else None


def _url_year(url):
    """URL 路径里的年份，如 /20230522/ 或 /2023/10/。"""
    m = re.search(r"/((?:19|20)\d{2})(?=[/_.-]|$)", str(url or ""))
    return int(m.group(1)) if m else None


def _page_date(page, url="", title=""):
    """网页发布日期。URL 日期 → 页面里的发布日期字段 → 标题里的年月 → 正文里的完整日期。

    日期字段常在页面后段（易车的 publishTime 在 15 万字符处），所以不再只扫开头；
    正文日期只扫前 8000 字符，避免把文末评论、相关推荐的日期当成发布日期。
    """
    m = re.search(r"/(20\d{2})(\d{2})(\d{2})(?:[/_.-]|$)", str(url or ""))
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            pass
    page = str(page or "")
    m = _PAGE_DATE_FIELD_RE.search(page)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            pass
    m = _PAGE_TITLE_YM_RE.search(str(title or ""))
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), 1).date()
    head = page[:8000]
    for pat in (_PAGE_FULL_DATE_RE, _PAGE_NUM_DATE_RE):
        m = pat.search(head)
        if not m:
            continue
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            continue
    return None


def _excerpt_year(excerpt):
    """摘录里最晚提到的年份，作为无发布日期的网页的发布时间估计。

    2023 年的文章不会写到 2026 年；反过来，2026 年的文章比较 2023 年数据很常见，
    所以取最晚年份而不是最早。
    """
    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", str(excerpt or ""))]
    years += [2000 + int(y) for y, _q in _PAGE_Q_RE.findall(str(excerpt or ""))]
    return max(years) if years else None


def _query_earliest(query):
    """检索式里点了年份就以最早那年的 1 月 1 日为下限，否则只收近两年的网页。"""
    years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", str(query or ""))]
    if years:
        return datetime(min(years), 1, 1).date()
    return (datetime.now() - timedelta(days=730)).date()


def _page_excerpt(url, query, cap=1200, require_number=False, title=""):
    """抓一页并摘录正文，返回（摘录, 发布日期）。日期取不到时返回 None。"""
    if _junk_url(url):
        return "", None
    try:
        with httpx.Client(timeout=20, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                          follow_redirects=True) as c:
            r = c.get(url)
    except httpx.HTTPError:
        return "", None
    if r.status_code != 200 or "pdf" in (r.headers.get("content-type") or "").lower():
        return "", None
    pub = _page_date(r.text, str(r.url), title)
    parser = _PageTextParser()
    try:
        parser.feed(r.text)
        text = parser.text()
    except Exception:
        text = ""
    if len(text) < 80:
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = _clean_web_text(html.unescape(text))
    if len(text) < 80:
        return "", pub
    keys = _query_keys(query)
    # 相关性闸门：检索词至少命中两条（单关键词时命中一条），否则是标题蹭词的无关页
    need = min(2, len(keys)) if keys else 0
    if need and sum(1 for k in keys if k in text.casefold()) < need:
        return "", pub

    def score(win):
        low = win.casefold()
        return sum(2 for k in keys if k in low) + min(
            len(re.findall(r"\d[\d,]*(?:\.\d+)?", win)), 20)

    def num_near_key(win):
        for k in keys:
            for m in re.finditer(re.escape(k), win, re.I):
                seg = win[max(0, m.start() - 120):m.end() + 120]
                if re.search(r"\d[\d,]*(?:\.\d+)?", seg):
                    return True
        return False

    low = text.casefold()
    starts = sorted({max(0, low.find(k) - 200) for k in keys if low.find(k) >= 0})
    if not starts:
        win = text[:cap]
        if require_number and (len(re.findall(r"\d[\d,]*(?:\.\d+)?", win)) < 2
                               or not num_near_key(win)):
            return "", pub
        return win, pub
    best = ""
    for start in starts:
        win = text[start:start + cap]
        if need and sum(1 for k in keys if k in win.casefold()) < need:
            continue
        if require_number:
            if len(re.findall(r"\d[\d,]*(?:\.\d+)?", win)) < 2 or not num_near_key(win):
                continue
        if not best or score(win) > score(best):
            best = win
    return best, pub


def _registry_hits(query, limit=2):
    """已登记的来源里挑和检索词沾边的，下次同类缺口优先复用。"""
    keys = _query_keys(query)
    if not keys:
        return []
    hits = []
    for s in known_web_sources():
        if _junk_url(s.get("url")):
            continue
        text = f"{s.get('title') or ''} {s.get('url') or ''}".casefold()
        if any(k in text for k in keys):
            hits.append({"title": s.get("title") or s["url"], "url": s["url"],
                         "snippet": "", "from": "registry"})
        if len(hits) >= limit:
            break
    return hits


def fetch_web_gap(query, limit=3, tid=None, targets=None):
    """专用源取不到时的兜底：检索网页，摘录落快照 DOCX 再摄入，锚点为快照块号。

    数据类缺口只接受带数字的正文页；全都不带数字就报错停回缺口关卡，不拿空话凑数。
    """
    query = _clean_web_text(query)
    want_num = bool(_DATA_HINT.search(query))
    earliest = _query_earliest(query)
    recent = datetime.now().date() - timedelta(days=365)
    reg = _registry_hits(query, limit=limit)
    if reg:
        tasks.update(tid, detail=f"复用已登记来源 {len(reg)} 个")
    res = fetch_web_search(query, limit=max(limit, 4))
    seen_urls = {r["url"] for r in reg}
    items = reg + [it for it in res["items"] if it["url"] not in seen_urls]
    tasks.update(tid, detail=f"待抓来源 {len(items)} 个，按相关性与数字密度筛正文")
    ranked, dropped_old = [], []
    for it in items:
        excerpt, pub = _page_excerpt(it["url"], query, require_number=want_num,
                                     title=it.get("title") or "")
        if not excerpt:
            continue
        # 检索词点了年份就按那年 1 月 1 日卡，没点年份只收近两年；旧文放进快照会把旧数据
        # 顶到「最新」位置，成文拿旧数当当前值
        if pub and pub < earliest:
            dropped_old.append((it["url"], pub.isoformat()))
            continue
        # 抓不到发布日期的页面（公众号、转载站常见）先看标题和 URL 里的年份，
        # 标题写着 2022 年的旧文不能因为正文提到 2025 就当新文；都没有年份再看摘录
        if not pub:
            year = _title_year(it.get("title")) or _url_year(it["url"]) or _excerpt_year(excerpt)
            if year and year < earliest.year:
                dropped_old.append((it["url"], f"约{year}"))
                continue
        low = excerpt.casefold()
        score = sum(2 for k in _query_keys(query) if k in low)
        score += min(len(re.findall(r"\d[\d,]*(?:\.\d+)?", excerpt)), 20)
        score += 6 if pub and pub >= recent else 0
        ranked.append((score, it, excerpt, pub))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if dropped_old:
        tasks.update(tid, detail=f"剔除以发布日早于 {earliest.isoformat()} 的网页 "
                                 f"{len(dropped_old)} 个")
    rows, used = [], []
    for _, it, excerpt, pub in ranked[:limit]:
        used.append(it)
        rows.append({"来源": it["title"], "网址": it["url"],
                     "发布日期": pub.isoformat() if pub else "未标注",
                     "检索词": query, "摘录": excerpt})
    if not rows:
        reason = ("抓到的网页都早于检索词年份下限 %s" % earliest.isoformat() if dropped_old
                  else ("数据类缺口没有抓到带数字的正文" if want_num else "没有抓到相关正文"))
        _remember_lesson(query, False, [], reason)
        raise FetchError(reason + "，停回缺口关卡，换检索词或上传材料")
    tasks.update(tid, detail="网页摘录落快照")
    note = (f"检索词：{query}；检索日：{config.today_str()}；发布日期下限：{earliest.isoformat()}"
            + (f"（已剔除 {len(dropped_old)} 个更早的网页）" if dropped_old else "")
            + "；正文为网页摘录，引用前请点原网址复核，数字以原网页为准。")
    snap = make_snapshot("网络检索", query, used[0]["url"], rows, note=note)
    r = ingest_snapshot(snap, tid, targets=targets)
    ok = bool(r) and "error" not in r and int((r or {}).get("entries_count") or 0) > 0
    _remember_lesson(query, ok, [_domain(it["url"]) for it in used],
                     "入库 %d 条" % int((r or {}).get("entries_count") or 0) if ok else "入库无条目")
    if ok:
        _remember_web_sources(used)
    summary = f"检索「{query}」取到 {len(rows)} 个来源，快照已落 {snap.name}"
    if ok:
        summary += f"，入库 {r.get('entries_count', 0)} 条"
    return {"summary": summary, "items": used, "snapshot": snap.name,
            "files": [snap.relative_to(config.ROOT).as_posix()],
            "entries_count": int((r or {}).get("entries_count") or 0),
            "ingest_failed": [] if ok else [snap.name]}


# ---------- 行业样板下载链（慧博免登录榜单前 3 → 东财下载，缺的按名次顺延） ----------

def top_brokers(industry, limit=3):
    """慧博新财富榜单里匹配行业组的前 N 家券商。"""
    ranking = fetch_hibor_ranking()
    group = match_group(ranking["items"], industry)
    if not group:
        return {"year": ranking["year"], "group": None, "brokers": []}
    brokers = sorted([it for it in ranking["items"] if it["group"] == group],
                     key=lambda x: x.get("rank") or 99)[:limit]
    return {"year": ranking["year"], "group": group, "brokers": brokers}


def sample_download_chain(industry, limit=3, tid=None, subject=None, skip_orgs=None, ingest=True):
    """样板链：慧博免登录取新财富榜单，按名次到东财下载综合最优研报，取满 limit 份。

    前 3 券商合计不足 limit 份时按榜单名次顺延（第 4、第 5…）补；
    同一家券商可出多份，不要求每家都有。
    榜单取不到或全组都取不到时退回东财热度兜底。skip_orgs 是库内已有样板的券商，跳过不重复下载。
    """
    industry = str(industry or "").strip()
    query = str(subject or industry).strip()
    if not query:
        raise FetchError("样板下载需要行业名或公司名，如 乘用车、吉利汽车")
    limit = max(1, min(int(limit or 3), 5))
    skip = {str(o) for o in (skip_orgs or []) if o}
    notes, pool = [], []
    try:
        ranking = fetch_hibor_ranking()
        group = match_group(ranking["items"], industry or query)
        if group:
            pool = sorted([it for it in ranking["items"] if it["group"] == group],
                          key=lambda x: x.get("rank") or 99)
            top = "、".join(f"{b['org']}（第{b['rank']}）" for b in pool[:3])
            notes.append(f"慧博 {ranking['year']} 年新财富「{group}」前 3：{top}")
        else:
            notes.append(f"慧博 {ranking['year']} 年榜单没有匹配到「{industry or query}」的行业组，改按东财热度找")
    except FetchError as e:
        notes.append(f"慧博榜单取不到（{e}），改按东财热度找")

    tasks.update(tid, detail="东财按榜单名次逐家下载样板")
    items = []
    for b in pool:
        need = limit - len(items)
        if need <= 0:
            break
        if b["org"] in skip:
            notes.append(f"{b['org']}：库内已有样板，跳过下载")
            continue
        try:
            one = fetch_eastmoney_reports(query, b["org"], limit=need, months=36, brokers_only=True)
        except FetchError as e:
            notes.append(f"{b['org']}（第{b['rank']}）：{e}，按榜单名次顺延")
            continue
        items += one["items"][:need]
        if one.get("skipped"):
            notes.append("跳过：" + "；".join(f"{s['title']}（{s['error']}）" for s in one["skipped"]))
    if 0 < len(items) < limit:
        try:
            more = fetch_eastmoney_reports(query, None, limit=limit - len(items),
                                           months=36, brokers_only=True)
            items += more["items"]
            notes.append("榜单券商取不满，按东财热度补足")
        except FetchError as e:
            notes.append(str(e))
    elif not items:
        res = fetch_eastmoney_reports(query, None, limit=limit, months=36, brokers_only=True)
        items = res["items"]
        if res.get("skipped"):
            notes.append("跳过：" + "；".join(f"{s['title']}（{s['error']}）" for s in res["skipped"]))
    ingested = {"classify": [], "ingest": []}
    if ingest:
        tasks.update(tid, detail=f"下载 {len(items)} 份样板，开始取名入库")
        ingested = ingest_inbox([it["file"] for it in items], tid)
    return {"champion": pool[0] if pool else None, "brokers": pool[:limit],
            "items": items, "notes": notes, "ingest": ingested}


# ---------- 任务入口 ----------

def _ingest_summary(ingested):
    ok = [r for r in (ingested.get("ingest") or []) if r and not r.get("error")]
    bad = [r for r in (ingested.get("ingest") or []) if r and r.get("error")]
    failed_classify = [r for r in (ingested.get("classify") or []) if r and not r.get("ok")]
    parts = []
    if ok:
        parts.append("入库：" + "、".join(f"{r['file']}（{r['entries_count']} 条）" for r in ok))
    if bad:
        parts.append("入库失败：" + "、".join(f"{r['file']}（{r['error']}）" for r in bad))
    if failed_classify:
        parts.append("取名失败：" + "、".join(f"{r['file']}（{r.get('error')}）" for r in failed_classify))
    return "；".join(parts)


def _ingest_failures(ingested):
    """下载成功但没能入库的文件与原因。取数缺口不能只看下载，材料没进库就取不到锚点。"""
    items = {}
    for r in ingested.get("classify") or []:
        if r and not r.get("ok"):
            items[Path(str(r.get("file") or "")).name] = str(r.get("error") or "取名失败")
    for r in ingested.get("ingest") or []:
        if r and r.get("error"):
            items[Path(str(r.get("file") or "")).name] = str(r.get("error"))
    return [f"{name}：{reason[:80]}" for name, reason in items.items() if name]


def _ingest_counts(ingested):
    """本次实际写进 wiki 的条目数；为 0 说明材料进了 raw 但没形成可用知识。"""
    return sum(int((r or {}).get("entries_count") or 0)
               for r in (ingested.get("ingest") or []) if r and not r.get("error"))


def _ingested_files(names):
    """按文件名回查清单里的相对路径，供成文选页时把新材料的页面带进上下文。"""
    wanted = {Path(str(n)).name for n in names if n}
    return [f["path"] for f in ingest_mod.load_manifest()["files"] if f.get("file") in wanted]


def _ingest_downloads(file_names, tid=None, targets=None, force_kind=None):
    """下载类源的入库入口：库内已有的文件（重复下载被 _download_pdf 复用）不再摄入，
    条目数按 manifest 记录累计，只有新文件走收件台摄入。"""
    by_name = {f.get("file"): f for f in ingest_mod.load_manifest()["files"]}
    fresh = [Path(n).name for n in file_names if Path(n).name not in by_name]
    known = [Path(n).name for n in file_names if Path(n).name in by_name]
    ingested = {"classify": [], "ingest": []}
    if fresh:
        ingested = ingest_inbox(fresh, tid, targets=targets, force_kind=force_kind)
    entries = _ingest_counts(ingested) + sum(
        int((by_name[n] or {}).get("entries_count") or 0) for n in known)
    return ingested, entries, known


def run_fetch(source, params, tid=None, targets=None):
    params = params or {}
    if source == "hibor_ranking":
        res = fetch_hibor_ranking(params.get("year"), params.get("bigtype") or 2)
        keyword = str(params.get("keyword") or "").strip()
        items = res["items"]
        if keyword:
            items = [it for it in items if keyword in it["group"]]
        champs = [it for it in items if it["rank"] == 1]
        if not champs:
            raise FetchError(f"慧博 {res['year']} 年{res['label']}榜单里没有匹配「{keyword}」的行业组")
        return {"summary": f"慧博 {res['year']} 年{res['label']}解析到 {len(champs)} 个行业组冠军",
                "items": champs}

    if source == "sample_chain":
        industry = params.get("industry")
        res = sample_download_chain(industry, limit=params.get("limit") or 3, tid=tid,
                                    skip_orgs=params.get("skip_orgs"),
                                    ingest=params.get("ingest", True))
        summary = "；".join(res["notes"])
        done = _ingest_summary(res["ingest"])
        if done:
            summary += "｜" + done
        return {"summary": summary, "items": res["items"], "champion": res["champion"],
                "files": _ingested_files([it.get("file") for it in res["items"]]),
                "entries_count": _ingest_counts(res["ingest"]),
                "ingest_failed": _ingest_failures(res["ingest"])}

    if source == "eastmoney_reports":
        res = fetch_eastmoney_reports(params.get("industry"), params.get("broker"),
                                      limit=params.get("limit") or 3,
                                      months=params.get("months") or 24,
                                      qtype=(params.get("qtype")
                                             if params.get("qtype") is not None else 1),
                                      stock_code=params.get("stock_code"),
                                      industry_code=params.get("industry_code"),
                                      org_code=params.get("org_code"),
                                      rating_change=params.get("rating_change"),
                                      page=params.get("page") or 1)
        files = [it["file"] for it in res["items"]]
        summary = "下载 " + "、".join(files)
        if res.get("resolved"):
            summary = f"股票 {res['resolved']}；" + summary
        if res.get("skipped"):
            summary += "；跳过 " + "、".join(s["title"] for s in res["skipped"])
        ingested, entries, known = {"classify": [], "ingest": []}, 0, []
        if params.get("ingest", True):
            ingested, entries, known = _ingest_downloads(files, tid, targets=targets)
        if known:
            summary += f"；{len(known)} 份库内已有、未重复下载"
        done = _ingest_summary(ingested)
        if done:
            summary += "｜" + done
        return {"summary": summary, "items": res["items"],
                "files": _ingested_files(files),
                "entries_count": entries,
                "ingest_failed": _ingest_failures(ingested)}

    if source == "cninfo_announce":
        res = fetch_cninfo_announce(params.get("query"), limit=params.get("limit") or 5,
                                    code=params.get("code"),
                                    category=params.get("category") or "",
                                    months=params.get("months") or 0,
                                    column=params.get("column") or "szse")
        files = [it["file"] for it in res["items"]]
        summary = "下载 " + "、".join(files)
        if res.get("skipped"):
            summary += "；跳过 " + "、".join(s["title"] for s in res["skipped"])
        ingested, entries, known = {"classify": [], "ingest": []}, 0, []
        if params.get("ingest", True):
            ingested, entries, known = _ingest_downloads(files, tid, targets=targets)
        if known:
            summary += f"；{len(known)} 份库内已有、未重复下载"
        done = _ingest_summary(ingested)
        if done:
            summary += "｜" + done
        return {"summary": summary, "items": res["items"],
                "files": _ingested_files(files),
                "entries_count": entries,
                "ingest_failed": _ingest_failures(ingested)}

    if source == "hkex_announce":
        res = fetch_hkex_announce(params.get("company"), params.get("code"),
                                  category=params.get("category") or "annual",
                                  months=params.get("months") or 0,
                                  title=params.get("title"),
                                  limit=params.get("limit") or 5)
        files = [it["file"] for it in res["items"]]
        summary = "下载 " + "、".join(files)
        if res.get("skipped"):
            summary += "；跳过 " + "、".join(s["title"] for s in res["skipped"])
        ingested, entries, known = {"classify": [], "ingest": []}, 0, []
        if params.get("ingest", True):
            ingested, entries, known = _ingest_downloads(files, tid, targets=targets)
        if known:
            summary += f"；{len(known)} 份库内已有、未重复下载"
        done = _ingest_summary(ingested)
        if done:
            summary += "｜" + done
        return {"summary": summary, "items": res["items"],
                "files": _ingested_files(files),
                "entries_count": entries,
                "ingest_failed": _ingest_failures(ingested)}

    if source in ("annual_report", "periodic_report"):
        if source == "annual_report":
            res = fetch_annual_report(params.get("company"), params.get("code"),
                                      params.get("market"), params.get("year"),
                                      limit=params.get("limit") or 1)
            summary = "年报下载 " + "、".join(it["file"] for it in res["items"])
        else:
            res = fetch_periodic_report(params.get("company"), params.get("code"),
                                        params.get("market"), params.get("kind") or "interim",
                                        days=params.get("days") or 400,
                                        limit=params.get("limit") or 1)
            summary = "定期报告下载 " + "、".join(it["file"] for it in res["items"])
        files = [it["file"] for it in res["items"]]
        ingested, entries, known = {"classify": [], "ingest": []}, 0, []
        if params.get("ingest", True):
            ingested, entries, known = _ingest_downloads(files, tid, targets=targets,
                                                         force_kind="filing")
        if known:
            summary += f"；{len(known)} 份库内已有、未重复下载"
        done = _ingest_summary(ingested)
        if done:
            summary += "｜" + done
        return {"summary": summary, "items": res["items"],
                "files": _ingested_files(files),
                "entries_count": entries,
                "ingest_failed": _ingest_failures(ingested)}

    if source == "web_search":
        return fetch_web_gap(params.get("query"), limit=params.get("limit") or 3, tid=tid,
                             targets=targets)

    if source == "stats_series":
        res = fetch_stats_series(params.get("keyword"), params.get("freq") or "auto",
                                 periods=params.get("periods") or 24,
                                 region=params.get("region") or "全国")
        return _snapshot_result(
            "国家统计局", str(res["indicator"] or res["keyword"]), res["url"], res["rows"],
            note=f"统计口径：{res['indicator']}；地区：{res['region']}；"
                 f"实际频率：{res.get('freq') or '—'}；时间区间 {res['dts']}。",
            tid=tid, targets=targets)

    if source == "akshare_series":
        payload = macro_via_akshare(params.get("series"), periods=params.get("periods") or 24)
        rows = _akshare_rows(payload)
        topic = AKSHARE_SERIES.get(payload["series"], payload["series"])
        warns = []
        if not payload.get("ordered"):
            warns.append("该序列时点未能解析排序，按原序截取，取到的可能不是最新期")
        stale = _akshare_stale_note(rows)
        if stale:
            warns.append(stale)
        note = (f"序列：{payload['series']}；按时间排序后取最近 {len(rows)} 个数值列行；"
                "数据经 AKShare 聚合，引用级数字建议再用原始接口核验。")
        if warns:
            note += "注意：" + "；".join(warns) + "。"
        return _snapshot_result(
            "AKShare", topic, f"AKShare 接口 {payload['series']}（聚合公开源）", rows,
            note=note, tid=tid, targets=targets)

    if source == "cpca_sales":
        res = fetch_cpca_sales(params.get("charttype") or 1, months=params.get("months") or 12,
                               metric=params.get("metric") or "全部", top=params.get("top") or 15)
        return _snapshot_result(
            "乘联会", res["label"], res["url"], res["rows"],
            note=f"图表类型：{res['label']}；数值单位见行内；乘联会口径与中汽协产销量不可直接混用。",
            tid=tid, targets=targets)

    if source == "csindex_index":
        res = fetch_csindex_snapshot(params.get("code"))
        return _snapshot_result(
            "中证指数", f"{res['name']}（{res['code']}）", res["url"], res["rows"],
            note="基础信息、最新行情、估值、收益率、前十大权重与行业权重取自中证指数官网；"
                 "估值与权重按官网最新披露日。", tid=tid, targets=targets)

    if source == "pbc_stat":
        res = fetch_pbc_stat(params.get("dataset") or "货币统计概览",
                             keyword=params.get("keyword"),
                             months=params.get("months") or 12)
        return _snapshot_result(
            "中国人民银行", f"{res['dataset']}·{res['title']}", res["url"], res["rows"],
            note=f"人民银行统计数据，表名：{res['title']}；单位随行标注；月度值以官网 Excel 为准。",
            tid=tid, targets=targets)

    if source == "fred_series":
        res = fetch_fred_series(params.get("series_id"), periods=params.get("periods") or 24)
        return _snapshot_result(
            "FRED", res["series"], res["url"], res["rows"],
            note="FRED 由圣路易斯联储维护；序列口径与频率见 FRED 页面。", tid=tid, targets=targets)

    if source == "imf_series":
        res = fetch_imf_series(params.get("indicator"), countries=params.get("countries") or "CHN",
                               periods=params.get("periods") or 24)
        return _snapshot_result(
            "IMF", f"{res['indicator']}（{res['countries']}）", res["url"], res["rows"],
            note="IMF DataMapper 年度值；指标定义见 IMF 数据映射器页面。", tid=tid, targets=targets)

    if source == "dbnomics_series":
        res = fetch_dbnomics_series(params.get("series"), periods=params.get("periods") or 24)
        return _snapshot_result(
            "DBnomics", res["series"], res["url"], res["rows"],
            note="DBnomics 聚合多个统计机构；同一序列的期次与单位见原始数据集。",
            tid=tid, targets=targets)

    if source == "worldbank_series":
        res = fetch_worldbank_series(params.get("indicator"),
                                     countries=params.get("countries") or "CHN",
                                     periods=params.get("periods") or 24)
        return _snapshot_result(
            "World Bank", f"{res['indicator']}（{res['countries']}）", res["url"], res["rows"],
            note="World Bank 年度值；指标口径见 World Bank Indicators 页面。",
            tid=tid, targets=targets)

    if source == "caam_article":
        res = fetch_caam_article(params.get("keyword"), category=params.get("category") or "行业要闻",
                                 limit=params.get("limit") or 1)
        paragraphs = []
        for it in res["items"]:
            paragraphs.append(f"【{it['title']}】")
            paragraphs.extend(it["paragraphs"])
        topic = res["items"][0]["title"] if len(res["items"]) == 1 else f"中汽协{res['category']}摘录"
        return _text_snapshot_result(
            "中国汽车工业协会", topic, res["items"][0]["url"], paragraphs,
            tid=tid, targets=targets,
            note="中汽协网页文字稿摘录；产销月报正文为图片的文章无法取数。")

    raise FetchError(f"未知取数源：{source}")


# ---------- 网页表单用源清单 ----------

# 网页「主动取数」只暴露这 5 个源；其余（AKShare/乘联会/中汽协/年报/定期报告/榜单/
# 中证/人行/FRED/IMF/DBnomics/世界银行/网络检索）保留给研报管线内部补缺口，
# 不出现在取数页（AKShare 的排序与停更修复仍要维护，管线缺口还在用它）。
MANUAL_SOURCE_IDS = (
    "stats_series", "eastmoney_reports", "cninfo_announce", "hkex_announce", "sample_chain",
)


def _stats_freq_map():
    """预置指标的频率联动表：{指标: {地区: [频率...]}}，供前端级联下拉。
    自定义指标不挑地区，用 * 通配回原有 auto/月/季/年 全集。"""
    out = {}
    for label, p in STATS_PRESETS.items():
        out[label] = {r: (p["freqs"].get(r) or p["freqs"].get("*") or [])
                      for r in p["regions"]}
    out["自定义"] = {"*": ["auto", "月", "季", "年"]}
    return out


def source_specs(only=None):
    specs = [
        {
            "id": "web_search", "label": "网络检索·外部来源兜底",
            "desc": "专用源取不到时用。免登录检索网页，正文摘录落快照 DOCX 并自动摄入；"
                    "已抓过的来源记入 data/web_sources.json，同类检索优先复用。",
            "params": [
                {"name": "query", "label": "检索词", "type": "text", "default": "", "required": True},
                {"name": "limit", "label": "来源数", "type": "number", "default": 3},
            ],
        },
        {
            "id": "stats_series", "label": "国家统计局·宏观序列",
            "desc": "5 个预置指标按实测口径联动地区与频率；选自定义仍按关键词搜，多个指标分号分隔。"
                    "本接口没有月度 CPI/PPI/社零序列；月度宏观由研报管线补缺口时自动经内部 "
                    "AKShare 源取，或到会话派资料员。",
            "params": [
                {"name": "keyword", "label": "指标", "type": "select",
                 "options": list(STATS_PRESETS.keys()) + ["自定义"],
                 "default": next(iter(STATS_PRESETS)), "required": True},
                {"name": "region", "label": "地区", "type": "select", "default": "全国",
                 "options_depends": {"fields": ["keyword"],
                                     "map": {label: p["regions"]
                                             for label, p in STATS_PRESETS.items()}
                                     | {"自定义": None}}},
                {"name": "freq", "label": "频率", "type": "select", "default": "",
                 "options_depends": {"fields": ["keyword", "region"],
                                     "map": _stats_freq_map()}},
                {"name": "periods", "label": "期数", "type": "number", "default": 24},
            ],
        },
        {
            "id": "akshare_series", "label": "AKShare·宏观序列",
            "desc": "在 conda macro-data 环境跑 AKShare；序列清单按实际可无参调用的 macro_china_* 函数整理。",
            "params": [
                {"name": "series", "label": "序列", "type": "select",
                 "options": list(AKSHARE_SERIES.keys()),
                 "labels": AKSHARE_SERIES, "default": "macro_china_gdp"},
                {"name": "periods", "label": "行数", "type": "number", "default": 24},
            ],
        },
        {
            "id": "eastmoney_reports", "label": "东财研报中心·下载研报",
            "desc": "选个股研报填股票名称或 6 位代码（名称自动解析成代码并在结果回显），"
                    "选行业研报填行业或主题；按深度与热度优先下载，默认自动取名入库。",
            "params": [
                {"name": "qtype", "label": "研报类型", "type": "select",
                 "options": ["1", "0"],
                 "labels": {"1": "行业研报", "0": "个股研报"}, "default": "1"},
                {"name": "industry", "label": "行业或主题", "type": "text", "default": "",
                 "when": {"field": "qtype", "in": ["1"]}},
                {"name": "stock_code", "label": "股票名称或股票代码", "type": "text", "default": "",
                 "when": {"field": "qtype", "in": ["0"]}, "required": True},
                {"name": "industry_code", "label": "东财行业代码（可空）", "type": "text", "default": "",
                 "when": {"field": "qtype", "in": ["1"]}},
                {"name": "org_code", "label": "机构代码（可空）", "type": "text", "default": ""},
                {"name": "broker", "label": "券商名（可空）", "type": "text", "default": ""},
                {"name": "months", "label": "回溯月数", "type": "number", "default": 24},
                {"name": "page", "label": "页码", "type": "number", "default": 1},
                {"name": "limit", "label": "份数", "type": "number", "default": 3},
                {"name": "ingest", "label": "下载后自动取名入库", "type": "checkbox", "default": True},
            ],
        },
        {
            "id": "cninfo_announce", "label": "巨潮资讯·公告财报",
            "desc": "按关键词或股票代码检索 A 股（沪深北）公告，可按 26 类公告分类与最近月数筛；"
                    "港股公告不在巨潮，请用「港交所披露易·公告」或「公司年报」。",
            "params": [
                {"name": "query", "label": "检索词", "type": "text", "default": ""},
                {"name": "code", "label": "股票代码（可空）", "type": "text", "default": ""},
                {"name": "category", "label": "公告分类", "type": "select",
                 "options": list(CNINFO_CATEGORIES.keys()),
                 "labels": CNINFO_CATEGORIES, "default": ""},
                {"name": "months", "label": "最近月数（0 为不限）", "type": "number", "default": 0},
                {"name": "column", "label": "市场", "type": "select",
                 "options": ["szse", "sse", "bse"], "default": "szse"},
                {"name": "limit", "label": "份数", "type": "number", "default": 5},
                {"name": "ingest", "label": "下载后自动取名入库", "type": "checkbox", "default": True},
            ],
        },
        {
            "id": "hkex_announce", "label": "港交所披露易·公告",
            "desc": "按港股代码取公告；分类含年报、中期、季度、ESG、业绩公告，可按最近月数、标题关键词筛。",
            "params": [
                {"name": "company", "label": "公司名或代码", "type": "text", "default": "", "required": True},
                {"name": "code", "label": "港股代码（可空，如 00700）", "type": "text", "default": ""},
                {"name": "category", "label": "公告分类", "type": "select",
                 "options": list(HKEX_CATEGORIES.keys()),
                 "labels": {k: v[3] for k, v in HKEX_CATEGORIES.items()}, "default": "annual"},
                {"name": "months", "label": "最近月数（0 为不限）", "type": "number", "default": 0},
                {"name": "title", "label": "标题关键词（可空）", "type": "text", "default": ""},
                {"name": "limit", "label": "份数", "type": "number", "default": 5},
                {"name": "ingest", "label": "下载后自动取名入库", "type": "checkbox", "default": True},
            ],
        },
        {
            "id": "annual_report", "label": "公司年报·巨潮 / HKEXnews",
            "desc": "按公司名或代码取最近一期年度报告，A 股走巨潮、港股走 HKEXnews，下载后自动入库。",
            "params": [
                {"name": "company", "label": "公司名", "type": "text", "default": "", "required": True},
                {"name": "code", "label": "股票代码（可空）", "type": "text", "default": ""},
                {"name": "market", "label": "市场", "type": "select", "options": ["", "a", "hk"], "default": ""},
                {"name": "year", "label": "年份（可空）", "type": "number", "default": 0},
                {"name": "limit", "label": "份数", "type": "number", "default": 1},
                {"name": "ingest", "label": "下载后自动取名入库", "type": "checkbox", "default": True},
            ],
        },
        {
            "id": "periodic_report", "label": "公司定期报告·巨潮 / HKEXnews",
            "desc": "按公司名取最近一期半年报或季报，A 股走巨潮、港股走 HKEXnews，下载后按财报结构入库。",
            "params": [
                {"name": "company", "label": "公司名", "type": "text", "default": "", "required": True},
                {"name": "code", "label": "股票代码（可空）", "type": "text", "default": ""},
                {"name": "market", "label": "市场", "type": "select", "options": ["", "a", "hk"], "default": ""},
                {"name": "kind", "label": "报告类型", "type": "select",
                 "options": ["interim", "q1", "q3", "quarterly"],
                 "labels": {"interim": "半年报", "q1": "一季报", "q3": "三季报", "quarterly": "季报（港股）"},
                 "default": "interim"},
                {"name": "days", "label": "回溯天数", "type": "number", "default": 400},
                {"name": "limit", "label": "份数", "type": "number", "default": 1},
                {"name": "ingest", "label": "下载后自动入库", "type": "checkbox", "default": True},
            ],
        },
        {
            "id": "hibor_ranking", "label": "慧博·分析师榜单",
            "desc": "免登录读榜单页，可选金牛、新财富、水晶球三类奖项；返回行业组冠军，可按行业关键词过滤。",
            "params": [
                {"name": "year", "label": "年份", "type": "number", "default": datetime.now().year - 1},
                {"name": "bigtype", "label": "榜单", "type": "select",
                 "options": ["2", "1", "3"],
                 "labels": {k: v for k, v in HIBOR_BIGTYPE.items()}, "default": "2"},
                {"name": "keyword", "label": "行业关键词（可空）", "type": "text", "default": ""},
            ],
        },
        {
            "id": "sample_chain", "label": "行业样板下载链",
            "desc": "慧博免登录取新财富榜单定行业组前 3 → 东财逐家下载综合最优 1 篇，可选下载后自动取名入库；"
                    "某家在东财没有时按榜单名次顺延补足。",
            "params": [
                {"name": "industry", "label": "行业", "type": "text", "default": "", "required": True},
                {"name": "limit", "label": "份数", "type": "number", "default": 3},
                {"name": "ingest", "label": "下载后自动取名入库", "type": "checkbox", "default": True},
            ],
        },
        {
            "id": "cpca_sales", "label": "乘联会·汽车销量",
            "desc": "data.cpcadata.com 的 chartlist 接口；1 整体市场、2 厂商、3 车型大类、4 国别、5 级别、6 新能源。",
            "params": [
                {"name": "charttype", "label": "图表类型", "type": "select",
                 "options": ["1", "2", "3", "4", "5", "6"],
                 "labels": {str(k): v for k, v in CPCA_CHARTS.items()}, "default": "1"},
                {"name": "metric", "label": "指标（可空=全部）", "type": "select",
                 "options": ["全部", "产量", "批发", "零售", "出口",
                             "当月销量", "累计销量", "当月份额", "累计份额"], "default": "全部"},
                {"name": "months", "label": "最近月数（厂商榜不适用）", "type": "number", "default": 12},
                {"name": "top", "label": "厂商榜取前几家（仅厂商榜）", "type": "number", "default": 15},
            ],
        },
        {
            "id": "csindex_index", "label": "中证指数·指数全貌",
            "desc": "按指数代码取基础信息、最新行情、PE/PB/股息率、区间收益、前十大权重与行业权重。",
            "params": [
                {"name": "code", "label": "指数代码", "type": "text", "default": "000300", "required": True},
            ],
        },
        {
            "id": "pbc_stat", "label": "人民银行·统计表",
            "desc": "从人行统计页按月挂的 Excel 取表；按数据集与表名关键词选最新一期，解析成指标行。",
            "params": [
                {"name": "dataset", "label": "数据集", "type": "select",
                 "options": list(PBC_PAGES.keys()), "default": "货币统计概览"},
                {"name": "keyword", "label": "表名关键词（可空=最新一张）", "type": "text", "default": ""},
                {"name": "months", "label": "最近月数", "type": "number", "default": 12},
            ],
        },
        {
            "id": "fred_series", "label": "FRED·美国宏观序列",
            "desc": "圣路易斯联储 FRED 的 CSV 接口，按序列号取观测值，如 GDPC1、CPIAUCSL、DGS10。",
            "params": [
                {"name": "series_id", "label": "FRED 序列号", "type": "text", "default": "GDPC1", "required": True},
                {"name": "periods", "label": "期数", "type": "number", "default": 24},
            ],
        },
        {
            "id": "imf_series", "label": "IMF·DataMapper",
            "desc": "IMF DataMapper 接口，按指标代码与国家代码取年度值，如 NGDP_RPCH / CHN,USA。",
            "params": [
                {"name": "indicator", "label": "指标代码", "type": "text", "default": "NGDP_RPCH", "required": True},
                {"name": "countries", "label": "国家代码（逗号分隔）", "type": "text", "default": "CHN"},
                {"name": "periods", "label": "期数", "type": "number", "default": 24},
            ],
        },
        {
            "id": "dbnomics_series", "label": "DBnomics·序列",
            "desc": "DBnomics v22 接口，序列写成 provider/dataset/series，可一次取多条用 + 连接。",
            "params": [
                {"name": "series", "label": "序列", "type": "text",
                 "default": "IMF/WEO:2024-10/CHN.NGDP_RPCH", "required": True},
                {"name": "periods", "label": "期数", "type": "number", "default": 24},
            ],
        },
        {
            "id": "worldbank_series", "label": "World Bank·指标",
            "desc": "世界银行 Indicators API，按指标代码与国家代码取年度值，如 NY.GDP.MKTP.CD / CHN。",
            "params": [
                {"name": "indicator", "label": "指标代码", "type": "text",
                 "default": "NY.GDP.MKTP.CD", "required": True},
                {"name": "countries", "label": "国家代码（分号分隔）", "type": "text", "default": "CHN"},
                {"name": "periods", "label": "期数", "type": "number", "default": 24},
            ],
        },
        {
            "id": "caam_article", "label": "中汽协·行业要闻",
            "desc": "中汽协行业要闻与协会动态的文字稿，可摘录入库；产销数据文章正文是图片，"
                    "取不到数字，产销数据请用乘联会。",
            "params": [
                {"name": "keyword", "label": "标题关键词（可空=最新一篇）", "type": "text", "default": ""},
                {"name": "category", "label": "栏目", "type": "select",
                 "options": list(CAAM_CATES.keys()), "default": "行业要闻"},
                {"name": "limit", "label": "篇数", "type": "number", "default": 1},
            ],
        },
    ]
    if only is not None:
        keep = set(only)
        specs = [s for s in specs if s["id"] in keep]
    return specs
