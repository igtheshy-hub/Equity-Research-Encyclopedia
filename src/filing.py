"""财报专用摄入流程（schema/财报摄取-skill.md 的实现）。

四段式：定位（找报表页码与元数据）→ 抄录（LLM 逐科目转结构化行）→ 计算（代码勾稽与
派生指标，见 fin_calc）→ 落库（【数据】【指标】【管理层】行进公司页 + data/fin 结构化存储）。
判断类内容一律不产出，归问答层。
"""
import json
import re

from . import config, fin_calc, llm, tasks, wiki
from .classify import sanitize
from .ingest import _apply_create, _apply_merge, _write_source_page
from .pdf_extract import anchor_of

LOCATE_PROMPT = """你是财报结构化助手。下面是一份财报每页的开头文字（页码: 前80字），请定位各部分页码并提取元数据。

只输出 JSON，不要解释：
{
  "company": "公司规范名（如 比亚迪）",
  "code": "证券代码（如 002594），识别不出给空字符串",
  "period_type": "Q1 或 H1 或 Q3 或 FY（一季报/半年报/三季报/年报）",
  "period_end": "报告期截止日 YYYY-MM-DD",
  "audit_opinion": "审计意见类型：标准无保留意见/带强调事项段的无保留意见/保留意见/无法表示意见/未披露",
  "sections": {
    "key": [主要会计数据所在页码],
    "bs": [合并资产负债表页码], "is": [合并利润表页码], "cf": [合并现金流量表页码], "eq": [合并所有者权益变动表页码],
    "parent_bs": [母公司资产负债表页码], "parent_is": [母公司利润表页码], "parent_cf": [母公司现金流量表页码], "parent_eq": [母公司所有者权益变动表页码],
    "cf_supp": [现金流量表补充资料页码],
    "mda": [管理层讨论与分析/经营情况讨论页码]
  }
}
找不到的部分给空数组；页码取方括号里的数字。每张报表必须给完整页码区间（起止之间的所有页都列出，资产负债表、现金流量表常跨 2-3 页），只给首页会漏科目。

--- 页面索引开始 ---
{PAGEMAP}
--- 页面索引结束 ---"""

TRANSCRIBE_PROMPT = """你是财报抄录员。下面是财报报表页的文本与表格（[第N页] 标记页码）。逐行抄录报表正表的科目与金额。

规则：
- 只抄正表行（含「其中：」子项、合计与总计行），附注文字丢弃
- 金额照抄不改不汇总：value 输出纯数字（去掉千分位逗号，负数用 -，括号负数转负号），unit 填该表金额列披露的单位（元/千元/万元/百万元/亿元等）
- 科目名 item 照报表原文抄，不改名不翻译；每股收益等单位是元/股，比率类（如加权平均净资产收益率）单位是 %
- statement 取值：资产负债表 / 利润表 / 现金流量表 / 所有者权益变动表 / 主要会计数据
- scope 取值：合并 / 母公司（按表头判断，判断不出给合并）
- 所有者权益变动表只抄报告年度那张表（上年比较表不抄），且只抄这几项：未分配利润列的本年年初余额（item 写 期初未分配利润）与本年年末余额（item 写 未分配利润）、提取盈余公积、对所有者（或股东）的分配。定位方法：权益变动表每行数字依次为 股本/其他权益工具/资本公积/库存股/其他综合收益/专项储备/盈余公积/未分配利润/归母小计/少数股东/合计，未分配利润列就是「归属于母公司股东权益小计」左边紧邻的那个数；变动行括号负数照抄负号

只输出 JSON：{"rows": [{"statement": "...", "scope": "...", "item": "...", "value": 123, "unit": "元", "page": 12}]}
页码取 [第N页] 标记中的 N。

--- 报表页开始 ---
{PAGES}
--- 报表页结束 ---"""

QUOTES_PROMPT = """你是财报摘录员。下面是财报管理层讨论与分析部分的文本（[第N页] 标记页码）。摘录管理层关于经营情况、业绩指引、会计政策变更、风险因素的原话。

规则：
- 只摘管理层的实质性陈述，样板话、免责声明、目录性文字丢弃
- 每条不超过 80 字，原话照抄不改写
- context 取值：经营说明 / 业绩指引 / 会计政策 / 风险提示
- 最多 8 条，宁缺毋滥

只输出 JSON：{"quotes": [{"text": "...", "context": "...", "page": 12}]}
页码取 [第N页] 标记中的 N。

--- 材料开始 ---
{PAGES}
--- 材料结束 ---"""

STATEMENT_ORDER = {"资产负债表": 0, "利润表": 1, "现金流量表": 2, "所有者权益变动表": 3, "主要会计数据": 4}
BATCH_PAGES = 6


def _norm_key(s):
    return "".join(str(s or "").split())


def _pages_from(sections, *keys):
    out = []
    for k in keys:
        out.extend(sections.get(k) or [])
    return out


def _valid_pages(pages, count):
    seen = set()
    for p in pages:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= count and n not in seen:
            seen.add(n)
    return sorted(seen)


def _locate(path, units):
    pagemap = "\n".join(f"[{u['n']}] {u['text'][:80].replace(chr(10), ' ')}" for u in units if u["text"])
    out = llm.chat(
        [{"role": "user", "content": LOCATE_PROMPT.replace("{PAGEMAP}", pagemap)}],
        task="filing",
        response_json=True,
    )
    count = len(units)
    ptype = str(out.get("period_type") or "").strip().upper()
    if ptype not in ("Q1", "H1", "Q3", "FY"):
        ptype = ""
    period_end = str(out.get("period_end") or "").strip()
    if period_end[:4].isdigit():
        year = int(period_end[:4])
        month = period_end[5:7]
        by_month = {"03": "Q1", "06": "H1", "09": "Q3", "12": "FY"}.get(month, "")
        if by_month and ptype != by_month:
            ptype = by_month
    if not ptype or not year:
        raise ValueError(f"定位失败：识别不出报告期（period_end={period_end!r}）")
    company = str(out.get("company") or "").strip()
    if not company:
        raise ValueError("定位失败：识别不出公司名")
    sections = out.get("sections") or {}
    flat = {k: _valid_pages(sections.get(k) or [], count) for k in (
        "key", "bs", "is", "cf", "eq", "parent_bs", "parent_is", "parent_cf", "parent_eq", "cf_supp", "mda")}
    return {
        "company": company,
        "code": str(out.get("code") or "").strip(),
        "pk": fin_calc.period_key(ptype, year),
        "period_end": period_end,
        "audit_opinion": str(out.get("audit_opinion") or "未披露").strip()[:30],
        "sections": flat,
    }


def _tables_text(path, pages):
    # 只对定位到的报表页跑表格抽取，作为抄录的补充上下文；不动 units 缓存
    if path.suffix.lower() != ".pdf" or not pages:
        return ""
    import pdfplumber

    out = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for n in pages:
                if n > len(pdf.pages):
                    continue
                tables = pdf.pages[n - 1].extract_tables() or []
                for tb in tables:
                    for row in tb:
                        cells = [(c or "").strip() for c in row]
                        if any(cells):
                            out.append(" | ".join(cells))
    except Exception:
        return ""
    return "\n".join(out)


def _page_blocks(path, units, pages, with_tables=True):
    by_n = {u["n"]: u["text"] for u in units}
    tables = _tables_text(path, pages) if with_tables else ""
    blocks = []
    for i, n in enumerate(pages):
        text = f"[第{n}页]\n{by_n.get(n, '')}"
        if with_tables and tables:
            text += "\n[本页表格文本见下方表格区]"
        blocks.append(text)
    if with_tables and tables:
        blocks.append(f"[表格区]\n{tables}")
    return blocks


def _transcribe(path, units, meta, task_id=None):
    # 权益变动表单独成批：pdfplumber 对该表只能抽出无行标的碎列，表格区是噪声
    eq_pages = _pages_from(meta["sections"], "eq", "parent_eq")
    want = [n for n in _pages_from(meta["sections"], "key", "bs", "is", "cf", "cf_supp",
                                   "parent_bs", "parent_is", "parent_cf")
            if n not in eq_pages]
    batches = [(want[i:i + BATCH_PAGES], True) for i in range(0, len(want), BATCH_PAGES)]
    batches += [(eq_pages[i:i + BATCH_PAGES], False) for i in range(0, len(eq_pages), BATCH_PAGES)]
    cons_pages = set(_pages_from(meta["sections"], "key", "bs", "is", "cf", "eq", "cf_supp"))
    parent_pages = set(_pages_from(meta["sections"], "parent_bs", "parent_is", "parent_cf", "parent_eq"))
    rows = []
    for batch, with_tables in batches:
        tasks.check_cancel(task_id)
        blocks = _page_blocks(path, units, batch, with_tables=with_tables)
        out = llm.chat(
            [{"role": "user", "content": TRANSCRIBE_PROMPT.replace("{PAGES}", "\n\n".join(blocks))}],
            task="filing",
            response_json=True,
        )
        for r in out.get("rows") or []:
            if not isinstance(r, dict):
                continue
            page = r.get("page")
            try:
                page = int(page)
            except (TypeError, ValueError):
                page = batch[0] if batch else 0
            rows.append({
                "statement": str(r.get("statement") or "").strip(),
                "scope": "母公司" if str(r.get("scope") or "").find("母公司") >= 0 else "合并",
                "item": str(r.get("item") or "").strip(),
                "value": r.get("value"),
                "unit": str(r.get("unit") or "元").strip(),
                "page": page,
            })
    # scope 按页归属机械覆盖：LLM 常把母公司表判成合并（表头相似），页码归属是硬事实
    for r in rows:
        if r["page"] in parent_pages:
            r["scope"] = "母公司"
        elif r["page"] in cons_pages:
            r["scope"] = "合并"
    return rows


def _quotes(path, units, meta, task_id=None):
    mda = meta["sections"].get("mda") or []
    quotes = []
    for i in range(0, min(len(mda), 20), 10):
        tasks.check_cancel(task_id)
        batch = mda[i:i + 10]
        blocks = _page_blocks(path, units, batch, with_tables=False)
        try:
            out = llm.chat(
                [{"role": "user", "content": QUOTES_PROMPT.replace("{PAGES}", "\n\n".join(blocks))}],
                task="filing",
                response_json=True,
            )
        except llm.LLMError:
            continue
        for q in out.get("quotes") or []:
            text = str(q.get("text") or "").strip()
            if not text:
                continue
            try:
                page = int(q.get("page"))
            except (TypeError, ValueError):
                page = batch[0] if batch else 0
            quotes.append({"text": text[:120], "context": str(q.get("context") or "经营说明").strip()[:8], "page": page})
    return quotes[:8]


_UNIT_PAT = re.compile(r"单位[为是为：:]*\s*(?:人民币)?(元|千元|万元|百万元|亿元)")

# 权益变动表关键行标签 → 规范科目名（分配类各行并入同一科目求代数和）
_EQ_LINE_LABELS = (
    ("年初余额", "期初未分配利润"),
    ("本年年末余额", "未分配利润"),
    ("提取盈余公积", "提取盈余公积"),
    ("对所有者（或股东）的分配", "对所有者或股东的分配"),
    ("对股东的分配", "对所有者或股东的分配"),
    ("对其他权益工具持有者的分配", "对所有者或股东的分配"),
)
_STATEMENTS = ("资产负债表", "利润表", "现金流量表", "所有者权益变动表")


def _tok_val(tok):
    """权益变动表 token 判定：数字/括号负数→float，占位 -→'PLACE'，其他（行标签、附注）→None"""
    tok = tok.strip()
    if tok in ("-", "—", "‐"):
        return "PLACE"
    neg = tok.startswith("(") or tok.startswith("-")
    s = tok.strip("()（）-− ").replace(",", "")
    if not s or not s.replace(".", "").isdigit():
        return None
    v = float(s)
    return -v if neg else v


def _detect_unit(units, sections):
    """从资产负债表首页（或其上一页）机械解析报表单位声明（如「财务附注中报表的单位为：千元」）。
    只认资产负债表：主要会计数据页常以元列示，与报表单位不同源，不能混用。"""
    by_n = {u["n"]: u["text"] for u in units}
    pp = sections.get("bs") or []
    pages = {min(pp), min(pp) - 1} if pp else set()
    for n in sorted(p for p in pages if p > 0):
        m = _UNIT_PAT.search(by_n.get(n, ""))
        if m:
            return m.group(1)
    return None


def _eq_fallback(path, units, meta):
    """合并权益变动表关键行的机械兜底：未分配利润列 = 数据区倒数第 4 个 token
    （右端固定为 …未分配利润/归母小计/少数股东/合计）。LLM 抄列易错位，此函数覆盖其输出。"""
    by_n = {u["n"]: u["text"] for u in units}
    year = meta["period_end"][:4]
    rows = []
    for n in meta["sections"].get("eq") or []:
        text = by_n.get(n, "")
        page_year = next((m.group(1) for ln in text.splitlines()[:6]
                          if (m := re.fullmatch(r"\s*(20\d{2})年\s*", ln))), None)
        if page_year is not None and page_year != year:
            continue  # 上年比较表
        vals = {}
        for line in text.replace("( ", "(").replace(" )", ")").splitlines():
            if len(line) > 300:
                continue
            label = next((item for pat, item in _EQ_LINE_LABELS if pat in line), None)
            if label is None:
                continue
            toks = line.split()
            start = 0
            for i, tok in enumerate(toks):
                if _tok_val(tok) is None:
                    start = i + 1
            cols = [_tok_val(t) for t in toks[start:]]
            if len(cols) < 4 or any(c is None for c in cols):
                continue
            col = cols[-4]
            if col == "PLACE":
                col = 0.0
            vals[label] = vals.get(label, 0.0) + col if label == "对所有者或股东的分配" else col
        for item, v in vals.items():
            rows.append({"statement": "所有者权益变动表", "scope": "合并", "item": item,
                         "value": v, "unit": "", "page": n, "_fallback": True})
    return rows


def _normalize_rows(path, rows, global_unit=None):
    """归一科目名与单位，返回 (合并 items, 母公司 items, 丢弃数)。

    items 每项存 {v, a, unit, statement, listed}：
      - 金额统一换算成「元」，unit 记 "元"；
      - 百分数保留披露的百分点原值（15.31 就是 15.31%），unit 记 "%"；
      - 每股收益保留原值，unit 记 "元/股"；
      - 其他单位（股、件等）也保留原值与单位，不再静默丢弃。
    global_unit 为报表声明单位（机械解析），纯金额行以它为准——LLM 逐页填单位易整体看错。
    """
    cons, parent, dropped = {}, {}, 0
    for r in rows:
        unit = fin_calc.normalize_unit(r.get("unit"))
        if global_unit and r.get("statement") in _STATEMENTS \
                and not fin_calc.is_percent_unit(unit) and not fin_calc.is_per_share_unit(unit):
            unit = fin_calc.normalize_unit(global_unit)  # 纯金额行按报表声明单位
        elif r.get("_fallback"):
            unit = fin_calc.normalize_unit(global_unit) or "元"
        raw_v = fin_calc.parse_value(r["value"])
        if raw_v is None or not r["item"]:
            dropped += 1
            continue
        name, listed = fin_calc.normalize_item(r["item"])
        if fin_calc.is_percent_unit(unit):
            v, stored_unit = raw_v, "%"
        elif fin_calc.is_per_share_unit(unit):
            v, stored_unit = raw_v, "元/股"
        else:
            v = fin_calc.to_yuan(raw_v, unit)
            if v is None:
                # 非金额单位（股、件、天等）保留原值，展示时带上单位
                v, stored_unit = raw_v, unit or ""
            else:
                stored_unit = "元"
        bucket = parent if r["scope"] == "母公司" else cons
        if name in bucket:
            old = bucket[name]["v"]
            if old and v and not 0.1 <= abs(v / old) <= 10:
                print(f"  [警告] 同名科目量级冲突：{name} 已存 {old:,.0f}，新值 {v:,.0f}（第{r['page']}页），保留先入值")
        else:
            bucket[name] = {
                "v": v,
                "a": anchor_of(path, r["page"]),
                "unit": stored_unit,
                "statement": r["statement"],
                "listed": listed,
            }
    return cons, parent, dropped


def _render_lines(path, pk, cons, ind, anc, quotes):
    data_lines = []
    for name, spec in sorted(cons.items(), key=lambda kv: STATEMENT_ORDER.get(kv[1].get("statement"), 9)):
        body = fin_calc.format_item_value(spec["v"], spec.get("unit"))
        data_lines.append(f"【数据】{pk} 合并 {name} = {body} {spec['a']}")
    ind_lines = []
    for name, v in ind.items():
        # 按指标自身单位显示：ROE 是百分比、EPS 保留小数、DSO/CCC 是天，避免一律「元」
        body = fin_calc.format_indicator_value(name, v)
        anchors = "".join(anc.get(name) or [])
        ind_lines.append(f"【指标】{pk} {name} = {body} {anchors}".rstrip())
    quote_lines = [f"【管理层】「{q['text']}」——{q['context']} {anchor_of(path, q['page'])}" for q in quotes]
    return data_lines, ind_lines, quote_lines


def _resolve_company_page(company, code):
    """精确/别名/包含三级匹配现有公司页。返回 page dict 或 None。"""
    pages = wiki.load_pages()
    keys = {_norm_key(company)}
    if code:
        keys.add(_norm_key(code))
    for p in pages:
        if p["type"] != "company":
            continue
        meta_keys = {_norm_key(p["meta"].get("title"))}
        meta_keys.update(_norm_key(a) for a in (p["meta"].get("aliases") or []))
        if keys & meta_keys:
            return p
    for p in pages:
        if p["type"] != "company":
            continue
        t = _norm_key(p["meta"].get("title"))
        if t and (t in _norm_key(company) or _norm_key(company) in t):
            return p
    return None


def _load_store(slug):
    f = config.FIN_DIR / f"{slug}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"slug": slug, "periods": {}}


def _save_store(store):
    config.FIN_DIR.mkdir(parents=True, exist_ok=True)
    f = config.FIN_DIR / f"{store['slug']}.json"
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)


def ingest_filing(path, units, unit_kind, manifest, sha, task_id=None):
    """财报摄入主流程。返回 ingest_one 形状的结果 dict；勾稽失败返回 None（挂起）。"""
    tasks.check_cancel(task_id)
    meta = _locate(path, units)
    print(f"  [财报] {meta['company']} {meta['pk']}（{meta['period_end']}）审计意见：{meta['audit_opinion']}")
    tasks.check_cancel(task_id)
    rows = _transcribe(path, units, meta, task_id=task_id)
    global_unit = _detect_unit(units, meta["sections"])
    if global_unit:
        print(f"  [单位] 报表声明单位：{global_unit}")
        fb = _eq_fallback(path, units, meta)
        if fb:
            fb_items = {r["item"] for r in fb}
            rows = [r for r in rows if not (r["statement"] == "所有者权益变动表"
                                            and r["scope"] == "合并" and r["item"] in fb_items)]
            print(f"  [权益表] 机械兜底 {len(fb)} 行（覆盖 LLM 抄列）")
            rows.extend(fb)
    cons, parent, dropped = _normalize_rows(path, rows, global_unit)
    print(f"  [抄录] 合并 {len(cons)} 项、母公司 {len(parent)} 项，丢弃 {dropped} 行")
    if not cons:
        print("  [拒] 未抄录到任何合并报表科目，不摄入")
        return None

    tol = config.CFG["ingest"].get("filing_check_tol", 0.005)
    fails = fin_calc.check_reconciliations({k: v["v"] for k, v in cons.items()}, tol)
    if fails:
        for x in fails:
            print(f"  [勾稽失败] {x}")
        print("  [挂起] 整份财报数值不入库，请人工核对后重试")
        return None

    tasks.check_cancel(task_id)
    quotes = _quotes(path, units, meta, task_id=task_id)

    # 公司页定位与 fin 存储就位
    hit = _resolve_company_page(meta["company"], meta["code"])
    slug = hit["slug"] if hit else (sanitize(meta["company"]) or "未命名公司")
    store = _load_store(slug)
    store.setdefault("company", meta["company"])
    aliases = store.setdefault("aliases", [])
    if meta["code"] and meta["code"] not in aliases:
        aliases.append(meta["code"])
    periods = store["periods"]
    pk = meta["pk"]
    rel = path.relative_to(config.ROOT).as_posix()

    # 计算指标（含上年数据引用）
    periods[pk] = {
        "period_end": meta["period_end"],
        "audit_opinion": meta["audit_opinion"],
        "source": rel,
        "items": cons,
        "items_parent": parent,
        "quotes": quotes,
    }
    ind, anc, missing = fin_calc.compute_indicators(periods, pk)
    periods[pk]["indicators"] = {k: {"v": v, "a": anc.get(k) or []} for k, v in ind.items()}
    print(f"  [指标] 算出 {len(ind)} 个，缺失 {len(missing)} 个（缺上年数据或科目）")

    data_lines, ind_lines, quote_lines = _render_lines(path, pk, cons, ind, anc, quotes)
    lines = data_lines + ind_lines + quote_lines
    topic = {
        "title": meta["company"],
        "type": "company",
        "aliases": [meta["code"]] if meta["code"] else [],
        "summary": f"{meta['company']}财报结构化数据（{pk}）",
        "sections": {"经营与财务": lines},
    }
    if hit:
        _apply_merge(hit["path"], hit["meta"], hit["body"], topic, [rel], f"财报摄入 {path.name}")
        touched = [str(hit["meta"].get("title") or hit["slug"])]
        created = []
        print(f"  并入 {hit['slug']}（+{len(lines)} 行）")
    else:
        new_path, cnt = _apply_create({"action": "create", "type": "company"}, topic, [rel], f"财报摄入 {path.name}")
        touched = [meta["company"]]
        created = [meta["company"]]
        print(f"  新建 {new_path.relative_to(config.WIKI_DIR).as_posix()}（{cnt} 行）")

    _save_store(store)

    extra = [
        f"审计意见：{meta['audit_opinion']}",
        f"报告期：{pk}（截止 {meta['period_end']}）",
    ]
    unlisted = [k for k, v in cons.items() if not v.get("listed")]
    if unlisted:
        extra.append(f"字典外科目（保留原名）：{'、'.join(unlisted[:12])}{' 等' if len(unlisted) > 12 else ''}")
    if missing:
        extra.append(f"缺失指标（缺科目或上年数据）：{'、'.join(missing[:15])}{' 等' if len(missing) > 15 else ''}")
    _write_source_page(path, touched, meta["period_end"], len(lines), extra)

    manifest["files"].append({
        "file": path.name,
        "path": rel,
        "sha256": sha,
        "unit": unit_kind,
        "count": len(units),
        "report_date": meta["period_end"] or None,
        "source_type": "filing",
        "ingested_at": config.now_iso(),
        "topics_touched": touched,
        "entries_count": len(lines),
    })
    # 调试转储
    dump = config.EXTRACTS_DIR / (path.stem + ".filing.json")
    dump.write_text(json.dumps({
        "meta": meta, "rows_raw": rows, "cons": cons, "parent": parent,
        "fails": fails, "indicators": periods[pk]["indicators"],
        "missing": missing, "quotes": quotes,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    log = []
    if created:
        log.append("新建 " + "、".join(created) + f"（财报 {pk}）")
    else:
        log.append(f"并入 {touched[0]}（财报 {pk}，+{len(lines)} 行）")
    log.append(f"指标 {len(ind)} 个、缺失 {len(missing)} 个")
    return {"file": path.name, "entries_count": len(lines), "log": "；".join(log)}
