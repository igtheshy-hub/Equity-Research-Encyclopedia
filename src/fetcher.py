"""网页端免登录取数：慧博榜单、东财研报、统计局序列、巨潮公告、股票年报、AKShare 宏观序列。

口径：
- 只碰免登录源。慧博只借免登录榜单定新财富前 3 券商，不登录、不下载正文（2026-10-29 实测登录后
  详情页仍无 PDF 直链，登录功能已移除）；发现报告积分等登录墙一律 raise FetchError，提示手动上传
  或到会话派资料员，不自动填凭据、不碰验证码。
- 研报、公告与年报下载进 raw/inbox，取名与摄入复用 pipeline 的取名/摄入双线（借同名互斥锁）。
- 年报按公司名或代码取最近一期：A 股走巨潮 category_ndbg_szsh，港股走 HKEXnews 的 prefix.do +
  titleSearchServlet.do（t1code=40000），下载后自动入库。
- 统计局与 AKShare 的数字落成快照 DOCX（raw/reports/数据快照/），再直接摄入，锚点为 paraN。
"""
import html
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from . import config, ingest as ingest_mod, llm, snapshot, tasks
from .classify import sanitize, unique_target
from .pipeline import build_lines

HIBOR_AUTHOR_URL = "https://www.hibor.com.cn/author.html"
EASTMONEY_LIST_URL = "https://reportapi.eastmoney.com/report/list"
EASTMONEY_PDF_URL = "https://pdf.dfcfw.com/pdf/H3_{info_code}_1.pdf"
NBS_BASE = "https://data.stats.gov.cn/dg/website/publicrelease/web/external"
CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_BASE = "http://static.cninfo.com.cn/"
HKEX_PREFIX_URL = "https://www1.hkexnews.hk/search/prefix.do"
HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEX_PDF_BASE = "https://www1.hkexnews.hk"
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
    "macro_china_gdp": "GDP（季度）",
    "macro_china_pmi": "制造业 PMI（月度）",
    "macro_china_cpi_yearly": "CPI 同比（月度）",
    "macro_china_lpr": "LPR（月度）",
    "macro_china_shrzgm": "社会融资规模（月度）",
}


class FetchError(RuntimeError):
    pass


def _cfg():
    return config.CFG.get("fetcher") or {}


def _timeout():
    return float(_cfg().get("timeout_s") or 30)


def _delay():
    return float(_cfg().get("delay_s") or 1.5)


def _client():
    return httpx.Client(headers={"User-Agent": UA}, timeout=_timeout(),
                        follow_redirects=True)


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


def _post_json(url, payload, attempts=2):
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


def _decode(resp):
    raw = resp.content
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
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


def fetch_hibor_ranking(year=None):
    year = int(year or (datetime.now().year - 1))
    params = {"year": year, "bigtype": 2}  # bigtype=2 新财富榜单；1/3 是其他奖项
    try:
        with _client() as c:
            r = c.get(HIBOR_AUTHOR_URL, params=params)
            r.raise_for_status()
            html = _decode(r)
    except httpx.HTTPError as e:
        raise FetchError(f"慧博榜单页打不开：{_conn_hint(e)}")
    items = _parse_author_rows(html)
    if not items:
        raise FetchError(f"慧博 {year} 年榜单页解析为空，页面结构可能变了；到会话里让资料员看一眼")
    return {"year": year, "url": str(r.url), "items": items}


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

def fetch_eastmoney_reports(industry=None, broker=None, limit=3, months=24, brokers_only=False):
    limit = max(1, min(int(limit or 3), int(_cfg().get("eastmoney_max") or 3)))
    end = datetime.now()
    begin = end - timedelta(days=int(months or 24) * 31)
    params = {
        "industryCode": "*", "pageSize": 100, "industry": "*", "rating": "", "ratingChange": "",
        "beginTime": begin.strftime("%Y-%m-%d"), "endTime": end.strftime("%Y-%m-%d"),
        "pageNo": 1, "fields": "", "qType": 1, "orgCode": "", "code": "*", "rcode": "",
        "p": 1, "pageNum": 1, "pageNumber": 1,
    }
    data = _get_json(EASTMONEY_LIST_URL, params=params)
    rows = data.get("data") or []
    terms = _industry_terms(industry)
    picked = []
    for it in rows:
        title = str(it.get("title") or "")
        org = str(it.get("orgSName") or it.get("orgName") or "")
        ind = str(it.get("industryName") or "")
        if terms and not any(t in ind or t in title for t in terms):
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
        target = industry or "该行业"
        who = f"{broker}的" if broker else ""
        raise FetchError(f"东财研报中心没找到{who}「{target}」近 {months} 个月的研报")

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
            "title": title, "org": org, "date": date,
            "count": it.get("count"), "pages": it.get("attachPages"),
            "file": dest.name, "url": url,
        })
        if i < len(picked):
            time.sleep(_delay())
    if not downloaded:
        raise FetchError("研报都下载失败了：" + "；".join(f"{s['title']}（{s['error']}）" for s in skipped))
    return {"items": downloaded, "skipped": skipped, "url": EASTMONEY_LIST_URL}


def _download_pdf(url, stem):
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    name = unique_target(config.INBOX_DIR, f"{stem}.pdf")
    dest = config.INBOX_DIR / name
    try:
        with _client() as c:
            with c.stream("GET", url) as r:
                r.raise_for_status()
                first = True
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes():
                        if first:
                            if not chunk.startswith(b"%PDF"):
                                raise FetchError("下载内容不是 PDF（链接失效或需要登录）")
                            first = False
                        f.write(chunk)
        if not dest.stat().st_size:
            raise FetchError("下载到的 PDF 是空文件")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return dest


# ---------- 国家统计局 ----------

FREQ_CODE = {"月": "1", "季": "2", "年": "3"}


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


def _nbs_search(keyword):
    res = _get_json(f"{NBS_BASE}/query",
                    params={"search": keyword, "pagenum": 1, "pageSize": 5})
    data = res.get("data")
    if isinstance(data, dict):
        data = data.get("data") or data.get("list") or []
    return data or []


def fetch_stats_series(keyword, freq="auto", periods=24):
    keyword = str(keyword or "").strip()
    if not keyword:
        raise FetchError("统计局取数需要中文指标关键词，如 居民消费价格")
    items = _nbs_search(keyword)
    if not items:
        raise FetchError(f"统计局接口没有搜到「{keyword}」，换个更短的中文关键词再试")
    want = FREQ_CODE.get(freq)
    pool = [x for x in items if want and str(x.get("type_value")) == want] or items

    def _name_score(x):
        name = str(x.get("show_name") or "").strip()
        return (2 if name.startswith(keyword) else 0) + (1 if name.split("(")[0].strip() == keyword else 0)

    chosen = max(pool, key=_name_score)
    cid, indic_id = chosen.get("cid"), chosen.get("indic_id")
    if not cid or not indic_id:
        raise FetchError("统计局搜索结果缺少 cid / indicatorId，接口结构可能变了")
    dt_type = str(chosen.get("dt_type") or "MM")
    dts = _dts_range(chosen.get("dt"), dt_type, periods)
    payload = {"cid": cid, "indicatorIds": [indic_id],
               "das": [{"text": "全国", "value": "000000000000"}],
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
                "口径": str(chosen.get("show_name") or keyword).strip(),
                "时点": str(block.get("name") or block.get("code") or ""),
            })
    rows.reverse()
    if not rows:
        raise FetchError("统计局接口返回空数据（可能未发布，或口径编码变了）")
    return {"keyword": keyword, "indicator": chosen.get("show_name"), "dt_type": dt_type,
            "dts": dts, "rows": rows, "url": f"{NBS_BASE}/stream/esData"}


# ---------- 巨潮公告 ----------

def _clean_title(s):
    return re.sub(r"<[^>]+>", "", str(s or "")).strip()


def _cninfo_search(query, page_size=10, category=""):
    form = {"pageNum": 1, "pageSize": max(int(page_size or 10), 10), "column": "szse",
            "tabName": "fulltext", "plate": "", "stock": "", "searchkey": query,
            "secdate": "", "tdate": "", "type": "", "category": category,
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


def fetch_cninfo_announce(query, limit=5):
    query = str(query or "").strip()
    if not query:
        raise FetchError("巨潮公告检索需要关键词，如 比亚迪 年度报告")
    limit = max(1, min(int(limit or 5), 10))
    items = _cninfo_search(query, page_size=max(limit * 2, 10))
    if not items:
        raise FetchError(f"巨潮没有搜到「{query}」的公告")

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
        downloaded.append({"title": title, "org": sec, "date": date, "file": dest.name, "url": url})
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


def fetch_annual_report(company, code="", market="", year=None, limit=1):
    company = str(company or "").strip()
    code = str(code or "").strip().upper()
    market = str(market or "").lower()
    if not market:
        market = "hk" if code.endswith(".HK") else ("a" if code else "")
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

AKSHARE_SCRIPT = """
import contextlib, io, json
import akshare as ak
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    df = getattr(ak, {series!r})()
df = df.tail({rows})
cols = [str(c) for c in df.columns]
recs = json.loads(df.to_json(orient="records", force_ascii=False))
print("<<<JSON>>>")
print(json.dumps({{"columns": cols, "records": recs}}, ensure_ascii=False))
"""


def macro_via_akshare(series, periods=24):
    series = str(series or "").strip()
    if series not in AKSHARE_SERIES:
        raise FetchError(f"不在允许的 AKShare 序列清单里：{series}")
    py = _cfg().get("macro_python") or ""
    if not py or not Path(py).exists():
        raise FetchError(f"找不到 macro-data 环境的 python（config.json 的 fetcher.macro_python: {py or '未配置'}）")
    periods = max(1, min(int(periods or 24), 120))
    code = AKSHARE_SCRIPT.format(series=series, rows=periods)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run([py, "-c", code], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=_timeout(), env=env)
    except subprocess.TimeoutExpired:
        raise FetchError(f"AKShare 取数超时（{_timeout():.0f} 秒），稍后重试")
    if proc.returncode != 0:
        raise FetchError("AKShare 取数失败：" + (proc.stderr or proc.stdout or "未知错误").strip()[-300:])
    marker = "<<<JSON>>>"
    if marker not in proc.stdout:
        raise FetchError("AKShare 没有返回数据：" + proc.stdout.strip()[-200:])
    payload = json.loads(proc.stdout.split(marker, 1)[1].strip().splitlines()[0])
    return {"series": series, "columns": payload.get("columns") or [],
            "records": payload.get("records") or []}


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
    return out


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
WEB_SOURCES_PATH = config.DATA_DIR / "web_sources.json"


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
        if it.get("url") and it["url"] not in known:
            sources.append({"title": it.get("title") or "", "url": it["url"],
                            "added": config.today_str()})
            known.add(it["url"])
    WEB_SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    WEB_SOURCES_PATH.write_text(json.dumps({"sources": sources}, ensure_ascii=False, indent=2),
                                encoding="utf-8")


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
        items.append({"title": title, "url": url, "snippet": snippet})
        if len(items) >= limit:
            break
    return items


def fetch_web_search(query, limit=5):
    query = str(query or "").strip()
    if not query:
        raise FetchError("网络检索需要关键词")
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    try:
        with httpx.Client(timeout=20, headers=headers, follow_redirects=True) as c:
            r = c.get(BING_SEARCH_URL, params={"q": query, "count": max(10, limit * 2),
                                               "setlang": "zh-CN"})
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise FetchError(f"检索失败：{e}")
    items = _parse_bing(r.text, limit)
    if not items:
        raise FetchError(f"检索「{query}」没有结果")
    return {"query": query, "items": items}


def _page_excerpt(url, query, cap=1200):
    try:
        with httpx.Client(timeout=20, headers={"User-Agent": UA}, follow_redirects=True) as c:
            r = c.get(url)
    except httpx.HTTPError:
        return ""
    if r.status_code != 200 or "pdf" in (r.headers.get("content-type") or "").lower():
        return ""
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 80:
        return ""
    keys = [k for k in re.split(r"[\s，,、；;：:]+", query) if len(k) >= 2]
    hits = sorted({text.find(k) for k in keys if text.find(k) >= 0})
    # 标题和导航里也会命中关键词，跳过开头 300 字再定位，摘录更可能落在正文
    pos = next((h for h in hits if h > 300), hits[0] if hits else 0)
    start = max(0, pos - 200)
    return text[start:start + cap]


def _registry_hits(query, limit=2):
    """已登记的来源里挑和检索词沾边的，下次同类缺口优先复用。"""
    keys = [k for k in re.split(r"[\s，,、；;：:]+", str(query or "")) if len(k) >= 2]
    if not keys:
        return []
    hits = []
    for s in known_web_sources():
        text = f"{s.get('title') or ''} {s.get('url') or ''}"
        if any(k in text for k in keys):
            hits.append({"title": s.get("title") or s["url"], "url": s["url"],
                         "snippet": "", "from": "registry"})
        if len(hits) >= limit:
            break
    return hits


def fetch_web_gap(query, limit=3, tid=None, targets=None):
    """专用源取不到时的兜底：检索网页，摘录落快照 DOCX 再摄入，锚点为快照块号。"""
    reg = _registry_hits(query, limit=limit)
    if reg:
        tasks.update(tid, detail=f"复用已登记来源 {len(reg)} 个")
    res = fetch_web_search(query, limit=limit)
    seen_urls = {r["url"] for r in reg}
    items = reg + [it for it in res["items"] if it["url"] not in seen_urls]
    tasks.update(tid, detail=f"待抓来源 {len(items)} 个，抓正文")
    rows, used = [], []
    for it in items:
        excerpt = _page_excerpt(it["url"], query)
        if not excerpt:
            continue
        used.append(it)
        rows.append({"来源": it["title"], "网址": it["url"], "检索词": query, "摘录": excerpt})
        if len(rows) >= limit:
            break
    if not rows:
        raise FetchError("检索到的网页都抓不到正文")
    _remember_web_sources(used)
    tasks.update(tid, detail="网页摘录落快照")
    snap = make_snapshot("网络检索", query, used[0]["url"], rows,
                         note=f"检索词：{query}；检索日：{config.today_str()}；正文为网页摘录，"
                              "引用前请点原网址复核，数字以原网页为准。")
    r = ingest_snapshot(snap, tid, targets=targets)
    summary = f"检索「{query}」取到 {len(rows)} 个来源，快照已落 {snap.name}"
    ok = bool(r) and "error" not in r
    if ok:
        summary += f"，入库 {r.get('entries_count', 0)} 条"
    return {"summary": summary, "items": used, "snapshot": snap.name,
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


def sample_download_chain(industry, limit=3, tid=None, subject=None, skip_orgs=None):
    """样板链：慧博免登录取新财富榜单，按名次逐家到东财下载综合最优 1 篇。

    某家券商在东财没有该主题研报时按榜单名次顺延（第 4、第 5…）补足 limit 家；
    榜单取不到或全组都取不到时退回东财热度兜底。skip_orgs 是库内已有的券商，跳过不重复下载。
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
            top = "、".join(f"{b['org']}（第{b['rank']}）" for b in pool[:limit])
            notes.append(f"慧博 {ranking['year']} 年新财富「{group}」前 {min(limit, len(pool))}：{top}")
        else:
            notes.append(f"慧博 {ranking['year']} 年榜单没有匹配到「{industry or query}」的行业组，改按东财热度找")
    except FetchError as e:
        notes.append(f"慧博榜单取不到（{e}），改按东财热度找")

    tasks.update(tid, detail="东财按榜单名次逐家下载样板")
    items = []
    for b in pool:
        if len(items) >= limit:
            break
        if b["org"] in skip:
            notes.append(f"{b['org']}：库内已有样板，跳过下载")
            continue
        try:
            one = fetch_eastmoney_reports(query, b["org"], limit=1, months=36, brokers_only=True)
        except FetchError as e:
            notes.append(f"{b['org']}（第{b['rank']}）：{e}，按榜单名次顺延")
            continue
        items += one["items"]
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
    """下载成功但没能入库的文件名。取数缺口不能只看下载，材料没进库就取不到锚点。"""
    names = []
    for r in ingested.get("classify") or []:
        if r and not r.get("ok"):
            names.append(Path(str(r.get("file") or "")).name)
    for r in ingested.get("ingest") or []:
        if r and r.get("error"):
            names.append(Path(str(r.get("file") or "")).name)
    return [n for n in dict.fromkeys(names) if n]


def run_fetch(source, params, tid=None, targets=None):
    params = params or {}
    if source == "hibor_ranking":
        res = fetch_hibor_ranking(params.get("year"))
        champs = [it for it in res["items"] if it["rank"] == 1]
        return {"summary": f"慧博 {res['year']} 年榜单解析到 {len(champs)} 个行业组冠军", "items": champs}

    if source == "sample_chain":
        industry = params.get("industry")
        res = sample_download_chain(industry, limit=params.get("limit") or 3, tid=tid,
                                    skip_orgs=params.get("skip_orgs"))
        summary = "；".join(res["notes"])
        done = _ingest_summary(res["ingest"])
        if done:
            summary += "｜" + done
        return {"summary": summary, "items": res["items"], "champion": res["champion"],
                "ingest_failed": _ingest_failures(res["ingest"])}

    if source == "eastmoney_reports":
        res = fetch_eastmoney_reports(params.get("industry"), params.get("broker"),
                                      limit=params.get("limit") or 3)
        summary = "下载 " + "、".join(it["file"] for it in res["items"])
        if res.get("skipped"):
            summary += "；跳过 " + "、".join(s["title"] for s in res["skipped"])
        ingested = {"classify": [], "ingest": []}
        if params.get("ingest", True):
            ingested = ingest_inbox([it["file"] for it in res["items"]], tid, targets=targets)
            done = _ingest_summary(ingested)
            if done:
                summary += "｜" + done
        return {"summary": summary, "items": res["items"],
                "ingest_failed": _ingest_failures(ingested)}

    if source == "cninfo_announce":
        res = fetch_cninfo_announce(params.get("query"), limit=params.get("limit") or 5)
        summary = "下载 " + "、".join(it["file"] for it in res["items"])
        if res.get("skipped"):
            summary += "；跳过 " + "、".join(s["title"] for s in res["skipped"])
        ingested = {"classify": [], "ingest": []}
        if params.get("ingest", True):
            ingested = ingest_inbox([it["file"] for it in res["items"]], tid, targets=targets)
            done = _ingest_summary(ingested)
            if done:
                summary += "｜" + done
        return {"summary": summary, "items": res["items"],
                "ingest_failed": _ingest_failures(ingested)}

    if source == "annual_report":
        res = fetch_annual_report(params.get("company"), params.get("code"),
                                  params.get("market"), params.get("year"),
                                  limit=params.get("limit") or 1)
        summary = "年报下载 " + "、".join(it["file"] for it in res["items"])
        ingested = {"classify": [], "ingest": []}
        if params.get("ingest", True):
            ingested = ingest_inbox([it["file"] for it in res["items"]], tid, targets=targets,
                                    force_kind="filing")
            done = _ingest_summary(ingested)
            if done:
                summary += "｜" + done
        return {"summary": summary, "items": res["items"],
                "ingest_failed": _ingest_failures(ingested)}

    if source == "web_search":
        return fetch_web_gap(params.get("query"), limit=params.get("limit") or 3, tid=tid,
                             targets=targets)

    if source == "stats_series":
        res = fetch_stats_series(params.get("keyword"), params.get("freq") or "auto",
                                 periods=params.get("periods") or 24)
        tasks.update(tid, detail="统计序列落快照")
        snap = make_snapshot("国家统计局", str(res["indicator"] or res["keyword"]),
                             res["url"], res["rows"],
                             note=f"统计口径：{res['indicator']}；时间区间 {res['dts']}；全国。")
        r = ingest_snapshot(snap, tid, targets=targets)
        summary = f"{res['indicator']} 取到 {len(res['rows'])} 期（{res['dts']}），快照已落 {snap.name}"
        if r:
            summary += f"，入库 {r.get('entries_count', 0)} 条"
        return {"summary": summary, "rows": res["rows"], "snapshot": snap.name}

    if source == "akshare_series":
        payload = macro_via_akshare(params.get("series"), periods=params.get("periods") or 24)
        rows = _akshare_rows(payload)
        tasks.update(tid, detail="AKShare 序列落快照")
        topic = AKSHARE_SERIES.get(payload["series"], payload["series"])
        snap = make_snapshot("AKShare", topic, f"AKShare 接口 {payload['series']}（聚合公开源）", rows,
                             note=f"序列：{payload['series']}；取最近 {len(rows)} 个数值列行；"
                                  "数据经 AKShare 聚合，引用级数字建议再用统计局原始接口核验。")
        r = ingest_snapshot(snap, tid, targets=targets)
        summary = f"{topic} 取到 {len(rows)} 行，快照已落 {snap.name}"
        if r:
            summary += f"，入库 {r.get('entries_count', 0)} 条"
        return {"summary": summary, "snapshot": snap.name}

    raise FetchError(f"未知取数源：{source}")


# ---------- 网页表单用源清单 ----------

def source_specs():
    return [
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
            "desc": "按中文指标名搜序列，取最近若干期，落快照并自动摄入。",
            "params": [
                {"name": "keyword", "label": "指标关键词", "type": "text", "default": "居民消费价格", "required": True},
                {"name": "freq", "label": "频率", "type": "select", "options": ["auto", "月", "季", "年"], "default": "auto"},
                {"name": "periods", "label": "期数", "type": "number", "default": 24},
            ],
        },
        {
            "id": "akshare_series", "label": "AKShare·宏观序列",
            "desc": "在 conda macro-data 环境跑 AKShare，落快照并自动摄入。",
            "params": [
                {"name": "series", "label": "序列", "type": "select",
                 "options": list(AKSHARE_SERIES.keys()),
                 "labels": AKSHARE_SERIES, "default": "macro_china_gdp"},
                {"name": "periods", "label": "行数", "type": "number", "default": 24},
            ],
        },
        {
            "id": "eastmoney_reports", "label": "东财研报中心·下载研报",
            "desc": "免登录按行业/券商检索，按热度与深度优先下载，默认自动取名入库。",
            "params": [
                {"name": "industry", "label": "行业或主题", "type": "text", "default": "", "required": True},
                {"name": "broker", "label": "券商（可空）", "type": "text", "default": ""},
                {"name": "limit", "label": "份数", "type": "number", "default": 3},
                {"name": "ingest", "label": "下载后自动取名入库", "type": "checkbox", "default": True},
            ],
        },
        {
            "id": "cninfo_announce", "label": "巨潮资讯·公告财报",
            "desc": "免登录检索公告并下载 PDF，默认自动取名入库（财报会走财报摄取流程）。",
            "params": [
                {"name": "query", "label": "检索词", "type": "text", "default": "", "required": True},
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
            "id": "hibor_ranking", "label": "慧博·新财富分析师榜单",
            "desc": "免登录读榜单页，返回各行业组冠军团队（机构与分析师），不下载正文。",
            "params": [
                {"name": "year", "label": "年份", "type": "number", "default": datetime.now().year - 1},
            ],
        },
        {
            "id": "sample_chain", "label": "行业样板下载链",
            "desc": "慧博免登录取新财富榜单定行业组前 3 → 东财逐家下载综合最优 1 篇并入库；"
                    "某家在东财没有时按榜单名次顺延补足。",
            "params": [
                {"name": "industry", "label": "行业", "type": "text", "default": "", "required": True},
                {"name": "limit", "label": "份数", "type": "number", "default": 3},
            ],
        },
    ]
