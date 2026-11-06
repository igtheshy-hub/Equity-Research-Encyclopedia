"""财报专用摄入流程（本技能 SKILL.md 的实现，2026-11-06 自 src/filing.py 迁入）。

四段式：定位（找报表页码与元数据）→ 抄录（LLM 逐科目转结构化行）→ 计算（代码勾稽与
派生指标，见 src/fin_calc.py）→ 落库（【数据】【指标】【管理层】行进公司页 + data/fin 结构化存储）。
判断类内容一律不产出，归问答层。

脚本由网页/命令行摄入在同一进程内直接 import 调用，不单开子进程；wiki 锁、manifest 锁与
任务取消语义因此与主流程共用。src/filing.py 是兼容入口，把模块名指向本文件。
"""
import sys
from pathlib import Path

# 本文件在 <仓库根>/.claude/skills/finstmt-ingest/scripts/ 下，向上四级是仓库根
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import json  # noqa: E402
import re  # noqa: E402
import unicodedata  # noqa: E402

from src import anchor_match, config, fin_calc, llm, tasks, wiki  # noqa: E402
from src.classify import sanitize  # noqa: E402
from src.ingest import (_apply_create, _apply_merge, _dir_type, _write_source_page,  # noqa: E402
                        normalize_targets)
from src.pdf_extract import anchor_of  # noqa: E402

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
{TARGET_RULE}
--- 页面索引开始 ---
{PAGEMAP}
--- 页面索引结束 ---"""

TARGET_LOCATE_RULE = """
本次摄入带着研报的目标数据，在 JSON 里另加 "targets" 字段，定位这些数据出现在哪些页：
{TARGET_LIST}
格式：[{"item": "目标数据名（照抄清单）", "pages": [页码]}]。只给正文里明确出现该数据的页，封面、目录、标题页不算；找不到的给空数组。"""

TARGET_PROMPT = """你是财报取数员。下面是本次研报要用的目标数据清单，以及财报相关页的文本与表格（[第N页] 标记页码）。

目标数据清单：
{TARGETS}

规则：
- 只在原文里找目标数据，逐条抄录原文出现的数字，不计算、不换算、不推测
- 原文分年度、分季度、分口径列示的，拆成多条，口径写进 scope（如 2024年/2025年、合并/母公司、期末/本期）
- value 输出纯数字（去掉千分位逗号，负数带负号，括号负数转负号），unit 照原文写（元/千元/万元/亿元/%/万辆等）
- item 用目标清单里的说法；原文叫法不同但指向同一口径同一数据的，item 仍写目标说法，scope 里注明原文科目名
- quote 摘原文片段，不超过 60 字
- 找不到的目标不输出；不要用相近指标顶替，不要估算
只输出 JSON：{"data": [{"item": "...", "scope": "...", "value": 123, "unit": "元", "page": 12, "quote": "..."}]}
页码取 [第N页] 标记中的 N。

--- 材料开始 ---
{PAGES}
--- 材料结束 ---"""

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


def _locate(path, units, targets=None):
    targets = normalize_targets(targets)
    pagemap = "\n".join(f"[{u['n']}] {u['text'][:80].replace(chr(10), ' ')}" for u in units if u["text"])
    prompt = LOCATE_PROMPT.replace("{PAGEMAP}", pagemap)
    if targets:
        target_list = "\n".join(f"- {t['item']}" for t in targets)
        prompt = prompt.replace("{TARGET_RULE}", TARGET_LOCATE_RULE.replace("{TARGET_LIST}", target_list))
    else:
        prompt = prompt.replace("{TARGET_RULE}", "")
    out = llm.chat(
        [{"role": "user", "content": prompt}],
        task="filing",
        response_json=True,
    )
    count = len(units)
    ptype = str(out.get("period_type") or "").strip().upper()
    if ptype not in ("Q1", "H1", "Q3", "FY"):
        ptype = ""
    period_end = str(out.get("period_end") or "").strip()
    year = 0
    if period_end[:4].isdigit():
        year = int(period_end[:4])
        month = period_end[5:7]
        by_month = {"03": "Q1", "06": "H1", "09": "Q3", "12": "FY"}.get(month, "")
        if by_month and ptype != by_month:
            ptype = by_month
    if not ptype or not year:
        # 公告类材料（利润分配、差错更正等）没有报告期，也没有三张正表；
        # 不能当财报结构化，转文本编译入 source 页，否则整份拒收、缺口永远补不上
        secs = out.get("sections") or {}
        has_stmt = any(_valid_pages(secs.get(k) or [], count) for k in ("bs", "is", "cf"))
        head = " ".join(str(u.get("text") or "")[:200] for u in units[:2])
        looks_notice = any(w in path.name or w in head
                           for w in ("公告", "说明书", "报告书", "预案"))
        if not has_stmt and looks_notice:
            raise UnsupportedFiling("公告类材料无报告期与四表，按文本编译")
        raise ValueError(f"定位失败：识别不出报告期（period_end={period_end!r}）")
    company = str(out.get("company") or "").strip()
    if not company:
        raise ValueError("定位失败：识别不出公司名")
    sections = out.get("sections") or {}
    flat = {k: _valid_pages(sections.get(k) or [], count) for k in (
        "key", "bs", "is", "cf", "eq", "parent_bs", "parent_is", "parent_cf", "parent_eq", "cf_supp", "mda")}
    target_pages = []
    for t in out.get("targets") or []:
        if not isinstance(t, dict):
            continue
        item = str(t.get("item") or "").strip()
        pages = _valid_pages(t.get("pages") or [], count)
        if item and pages:
            target_pages.append({"item": item[:40], "pages": pages})
    return {
        "company": company,
        "code": str(out.get("code") or "").strip(),
        "pk": fin_calc.period_key(ptype, year),
        "period_end": period_end,
        "audit_opinion": str(out.get("audit_opinion") or "未披露").strip()[:30],
        "sections": flat,
        "target_pages": target_pages,
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


def _transcribe_batch(path, units, batch, with_tables=True):
    blocks = _page_blocks(path, units, batch, with_tables=with_tables)
    out = llm.chat(
        [{"role": "user", "content": TRANSCRIBE_PROMPT.replace("{PAGES}", "\n\n".join(blocks))}],
        task="filing",
        response_json=True,
    )
    rows = []
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
    return rows


_SCOPE_HEAD_RE = re.compile(
    r"(合并|母公司)\s*(?:资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表)")
# 德勤等版式把母公司表头写作「公司资产负债表」（不带母公司三字），独立成行才算
_BARE_HEAD_RE = re.compile(
    r"(?:公司)?\s*(?:资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表)\s*(?:\s*[-–—]+\s*续)?")


def _scope_marks(text):
    """页内「合并/母公司 × 报表名」标题位置。

    「公司资产负债表」这类不带母公司三字的表头只在页首独立成行时按母公司算，
    避免正文或目录里的散词把归属切错。"""
    marks = [(m.start(), "母公司" if m.group(1) == "母公司" else "合并")
             for m in _SCOPE_HEAD_RE.finditer(text)]
    lines = str(text or "").splitlines()
    for i, ln in enumerate(lines[:6]):
        if _BARE_HEAD_RE.fullmatch(ln.strip()):
            marks.append((sum(len(x) + 1 for x in lines[:i]), "母公司"))
    return sorted(marks)


def _item_offsets(text, item, value, unit):
    """科目名在页内的出现位置，分两档返回：(数值核得上的位置, 仅科目名对上的位置)。

    页文本里的「加：期初」「其中：子公司」带冒号等标点，科目名匹配时字符间
    允许空白与常用标点，否则带前缀的科目在声称页上找不到位置，归属会退化成
    按页首继承，把合并数与母公司数互换。"""
    head = re.sub(r"[（(].*?[）)]", "", str(item or ""))
    head = re.sub(r"[\s:：、，,；;]", "", head)[:8]
    if len(head) < 2:
        return [], []
    pat = r"[\s:：、，,；;]*".join(re.escape(ch) for ch in head)
    num = fin_calc.parse_value(value)
    hits = []
    for m in re.finditer(pat, text):
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        snippet = text[start:end if end >= 0 else len(text)]
        ok = num is not None and _value_on_page(snippet, item, num, unit)
        hits.append((m.start(), ok))
    return [pos for pos, ok in hits if ok], [pos for pos, _ in hits]


def _value_offset(text, value):
    """数值在页内的首个出现位置；权益变动表的行只有数字没有科目名，只能按数值定位。"""
    num = fin_calc.parse_value(value)
    if num is None:
        return None
    for v in (num, abs(num)):
        for fmt in ("{:,.2f}", "{:.2f}", "{:,.0f}", "{:.0f}"):
            m = re.search(r"(?<![\d.,])" + re.escape(fmt.format(v)) + r"(?![\d])", text)
            if m:
                return m.start()
    return None


def _mechanical_scopes(units, rows):
    """按页内「合并/母公司 × 报表名」标题的位置给每行定归属。

    季报常把合并表、母公司表甚至上年比较表排在同一页，只按页码归属会把母公司数
    抄进合并口径，勾稽因此不平。标题之前的行归前一张表，标题之后归新表。
    LLM 抄列还常把页码写偏一两页：声称页上核不到「科目名+数值」时，先去别的页
    找这对内容真正所在的位置，找不到才退回声称页的表头归属——否则母公司表的
    行只要页码写成合并表那页，就会按那页的表头被错标成合并口径。
    权益变动表的行名（期初未分配利润、未分配利润）在页上只有列标题没有行标，
    科目名核不到时改按数值在页内的位置定归属：同一页上「母公司所有者权益变动
    表」标题之后、却继承到上一页合并口径的行，会因此归错桶（茅台 2025 年报
    R3 挂起的根因）。"""
    texts = {u["n"]: unicodedata.normalize("NFKC", u["text"]) for u in units}
    marks = {n: _scope_marks(t) for n, t in texts.items()}
    page_start, cur = {}, None
    for n in sorted(texts):
        page_start[n] = cur
        if marks[n]:
            cur = marks[n][-1][1]

    def _scope_at(n, offs):
        for off in offs:
            before = [s for pos, s in marks.get(n, []) if pos <= off]
            if before:
                return before[-1]
        return None

    scopes = []
    for r in rows:
        n = r["page"]
        verified, by_name = _item_offsets(texts.get(n, ""), r["item"], r["value"], r["unit"])
        scope = _scope_at(n, verified)
        if scope is None:
            pos = _value_offset(texts.get(n, ""), r["value"])
            if pos is not None:
                scope = _scope_at(n, [pos])
        if scope is None and fin_calc.parse_value(r["value"]) is not None:
            for m in sorted(texts):
                if m == n:
                    continue
                v2, _ = _item_offsets(texts[m], r["item"], r["value"], r["unit"])
                if v2:
                    scope = _scope_at(m, v2)
                    if scope:
                        break
        if scope is None:
            scope = _scope_at(n, by_name) or page_start.get(n) or r["scope"]
        scopes.append(scope)
    return scopes


def _transcribe_pages(path, units, batch, with_tables):
    """一批页抄写失败（多为输出超过 max_tokens 被截断）时拆小重抄，单页仍失败向上抛。"""
    try:
        return _transcribe_batch(path, units, batch, with_tables)
    except llm.LLMParseError:
        if len(batch) <= 1:
            raise
        mid = len(batch) // 2
        print(f"  [分批] 第{batch[0]}-{batch[-1]}页抄录失败，拆成 {len(batch[:mid])}+{len(batch[mid:])} 页重抄")
        return (_transcribe_pages(path, units, batch[:mid], with_tables)
                + _transcribe_pages(path, units, batch[mid:], with_tables))


_HEAD_LINE_RE = re.compile(
    r"^(?:[（(][一二三四五六七八九十\d]+[）)]|[一二三四五六七八九十]+、|\d+[.、])?\s*"
    r"(合并|母公司|公司)\s*(资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表)$")
_SEC_FOR_STMT = {"资产负债表": "bs", "利润表": "is", "现金流量表": "cf",
                 "所有者权益变动表": "eq", "股东权益变动表": "eq"}


def _ensure_header_pages(units, meta):
    """报表首页常与「财务报表」「审计意见」同页，定位阶段容易漏掉，导致流动资产、
    收入前几行整段缺失。凡是页里有独立成行的「合并/母公司 × 报表名」标题、又不属于
    任何已定页的，补进对应报表页集合。德勤版式的「公司资产负债表」按母公司表算。"""
    secs = meta["sections"]
    have = set()
    for k in ("key", "bs", "is", "cf", "eq", "cf_supp",
              "parent_bs", "parent_is", "parent_cf", "parent_eq"):
        have.update(secs.get(k) or [])
    added = []
    for u in units:
        if u["n"] in have:
            continue
        for line in unicodedata.normalize("NFKC", u["text"]).splitlines():
            m = _HEAD_LINE_RE.match(line.strip())
            if not m:
                continue
            key = _SEC_FOR_STMT.get(m.group(2))
            if not key:
                continue
            sec = ("parent_" if m.group(1) in ("母公司", "公司") else "") + key
            secs.setdefault(sec, []).append(u["n"])
            secs[sec].sort()
            have.add(u["n"])
            added.append((u["n"], sec))
            print(f"  [定位] 补入第{u['n']}页（{sec} 表头漏定位）")
            break
    return added


def _transcribe(path, units, meta, task_id=None):
    _ensure_header_pages(units, meta)
    # 权益变动表单独成批：pdfplumber 对该表只能抽出无行标的碎列，表格区是噪声
    eq_pages = _pages_from(meta["sections"], "eq", "parent_eq")
    want = [n for n in _pages_from(meta["sections"], "key", "bs", "is", "cf", "cf_supp",
                                   "parent_bs", "parent_is", "parent_cf")
            if n not in eq_pages]
    batches = [(want[i:i + BATCH_PAGES], True) for i in range(0, len(want), BATCH_PAGES)]
    batches += [(eq_pages[i:i + BATCH_PAGES], False) for i in range(0, len(eq_pages), BATCH_PAGES)]
    rows = []
    for batch, with_tables in batches:
        tasks.check_cancel(task_id)
        rows.extend(_transcribe_pages(path, units, batch, with_tables))
    # scope 按页内表头位置机械覆盖：LLM 常把母公司表判成合并（表头相似），
    # 而定位阶段又可能漏掉母公司页，页码归属不如标题位置可靠
    for r, scope in zip(rows, _mechanical_scopes(units, rows)):
        r["scope"] = scope
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
            if not isinstance(q, dict):  # 嵌套层混进清单时跳过，不崩整份抄录
                continue
            text = str(q.get("text") or "").strip()
            if not text:
                continue
            try:
                page = int(q.get("page"))
            except (TypeError, ValueError):
                page = batch[0] if batch else 0
            quotes.append({"text": text[:120], "context": str(q.get("context") or "经营说明").strip()[:8], "page": page})
    return quotes[:8]


def _target_scan_pages(meta, count, cap=12):
    """目标数据扫描页：定位到的目标页优先，再补主要会计数据页与 MD&A 页。"""
    pages = []
    for t in meta.get("target_pages") or []:
        pages += t.get("pages") or []
    pages += meta["sections"].get("key") or []
    pages += meta["sections"].get("mda") or []
    return _valid_pages(pages, count)[:cap]


def _clean_target_item(s):
    s = re.sub(r"[\[\]【】=]", "", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip(" ：:，,。；;")
    return s[:40]


def _fmt_target_num(v):
    return f"{v:,.6f}".rstrip("0").rstrip(".")


def _target_rows(path, units, meta, targets):
    """目标数据补抄：按定位页、主要会计数据页与 MD&A 页找研报要的数据。

    标准科目字典之外的目标数据也在这里取值；找不到的目标不输出，不用相近指标顶替。
    """
    targets = normalize_targets(targets)
    if not targets:
        return []
    pages = _target_scan_pages(meta, len(units))
    if not pages:
        return []
    target_list = "\n".join(
        f"- {t['item']}" + (f"（用途：{t['why']}）" if t["why"] else "") for t in targets)
    rows, seen = [], set()
    for i in range(0, len(pages), BATCH_PAGES):
        batch = pages[i:i + BATCH_PAGES]
        blocks = _page_blocks(path, units, batch, with_tables=True)
        try:
            out = llm.chat(
                [{"role": "user", "content": TARGET_PROMPT.replace("{TARGETS}", target_list)
                  .replace("{PAGES}", "\n\n".join(blocks))}],
                task="filing",
                response_json=True,
            )
        except llm.LLMError as e:
            print(f"  [目标] 第 {i // BATCH_PAGES + 1} 批提取失败：{e}")
            continue
        for r in out.get("data") or []:
            if not isinstance(r, dict):
                continue
            item = _clean_target_item(r.get("item"))
            value = fin_calc.parse_value(r.get("value"))
            if not item or value is None:
                continue
            try:
                page = int(r.get("page"))
            except (TypeError, ValueError):
                page = batch[0]
            if page not in batch:
                page = batch[0]
            scope = re.sub(r"[（）()]", "", str(r.get("scope") or ""))[:20]
            unit = str(r.get("unit") or "").strip()[:10]
            quote = str(r.get("quote") or "").strip()[:80]
            key = (item, scope, page, value, unit)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"item": item, "scope": scope, "value": value, "unit": unit,
                         "page": page, "quote": quote, "anchor": anchor_of(path, page)})
    print(f"  [目标] 目标 {len(targets)} 项，抄到 {len(rows)} 条")
    return rows


def _drop_target_dupes(rows, cons):
    """与四表已抄科目同一口径同一数值的目标行略过，避免页面重复。"""
    out, dropped = [], 0
    for r in rows:
        name, _listed = fin_calc.normalize_item(r["item"])
        spec = cons.get(name)
        if spec and (not r["scope"] or "合并" in r["scope"]):
            v = fin_calc.to_yuan(r["value"], r["unit"])
            if v is not None and spec.get("v") and 0.995 <= abs(v / spec["v"]) <= 1.005:
                dropped += 1
                continue
        out.append(r)
    return out, dropped


def _render_target_lines(pk, rows):
    lines = []
    for r in rows:
        label = f"{r['item']}（{r['scope']}）" if r["scope"] else r["item"]
        if label.count("（") != label.count("）") or label.count("(") != label.count(")"):
            label = re.sub(r"[（）()]", "", label)
        unit = r["unit"]
        if unit in ("%", "％"):
            body = _fmt_target_num(r["value"]) + "%"
        elif unit:
            body = f"{_fmt_target_num(r['value'])} {unit}"
        else:
            body = _fmt_target_num(r["value"])
        lines.append(f"【数据】{pk} 目标 {label} = {body} {r['anchor']}")
    return lines


_UNIT_PAT = re.compile(r"单位[为是为：:]*\s*(?:人民币)?(元|千元|万元|百万元|亿元)")
# 港股报表常在表头写「人民幣千元」而不写「单位」，单独认这种声明
_UNIT_DECL_PAT = re.compile(r"(?:人民幣|人民币)\s*(百万元|百萬元|千元|万元|萬元|亿元|億元|元)")
_HKD_PAT = re.compile(r"港幣|港元|港币")


class UnsupportedFiling(RuntimeError):
    """财报格式超出 A 股四表流程（如港股以港元列示），交回 ingest 走文本编译。"""

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
        text = by_n.get(n, "")
        m = _UNIT_PAT.search(text) or _UNIT_DECL_PAT.search(text)
        if m:
            return m.group(1)
    return None


def _detect_currency(units, sections):
    """报表币种：港股年报常见港元列示，A 股四表流程按人民币元记账，港元版不能套用。"""
    by_n = {u["n"]: u["text"] for u in units}
    pp = sections.get("bs") or []
    if not pp:
        return "CNY"
    text = "\n".join(by_n.get(n, "") for n in {min(pp), min(pp) - 1} if n > 0)
    return "HKD" if _HKD_PAT.search(text) else "CNY"


def _supp_net_profit(units, meta, global_unit):
    """从现金流量表补充资料页机械取「净利润」行的本期金额（起点净利润）。
    取不到、报表单位未识别或季报未披露补充资料时返回 None，R4 跳过。"""
    pages = set(meta["sections"].get("cf_supp") or [])
    if not pages or not global_unit:
        return None
    for u in sorted(units, key=lambda x: x["n"]):
        if u["n"] not in pages:
            continue
        for raw in u["text"].splitlines():
            line = raw.strip()
            if not re.match(r"^净利润(?![一-鿿])", line):
                continue
            if "归属" in line or "少数股东" in line:
                continue
            vals = []
            for tok in line[len("净利润"):].split():
                v = _tok_val(tok)
                if v is None or v == "PLACE":
                    continue
                vals.append(v)
            if vals:
                return fin_calc.to_yuan(vals[0], global_unit)
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


_STMT_CLASS = {"资产负债表": "BS", "利润表": "IS", "现金流量表": "CF", "所有者权益变动表": "EQ"}


def _row_snippets(text, item):
    """按科目名截取所有出现处的所在行；科目名换行断开时把下一行也带上。"""
    text = unicodedata.normalize("NFKC", text)
    head = re.sub(r"[（(].*?[）)]", "", unicodedata.normalize("NFKC", str(item or "")))
    head = re.sub(r"[\s:：]", "", head)[:8]
    if len(head) < 2:
        return []
    out = []
    for m in re.finditer(r"\s*".join(re.escape(ch) for ch in head), text):
        start = text.rfind("\n", 0, m.start()) + 1
        end = text.find("\n", m.end())
        snippet = text[start:end if end >= 0 else len(text)]
        # 行首序号「1.」「（1）」不算金额；只有这种数字时把下一行带上，避免漏掉换行后的数值
        if not re.search(r"\d[\d,]*\.\d+|\d{2,}", snippet) and end >= 0:
            nxt = text.find("\n", end + 1)
            snippet = text[start:nxt if nxt >= 0 else len(text)]
        out.append(snippet)
    return out


def _value_on_page(text, item, value, unit):
    """抄录值必须真出现在所标页码的原文里；负数按绝对值再试，财报空值列用「-」记 0。"""
    if not text:
        return False
    cands = [(str(value), unit)]
    if value < 0:
        cands.append((str(abs(value)), unit))
    snippets = _row_snippets(text, item) or [text]
    if any(anchor_match.unit_has_term(s, t) for s in snippets for t in cands):
        return True
    if abs(value) < 10 and value == int(value):
        # number_terms 会把无单位的一位数当噪声滤掉，小额行在行内按严格边界单独认一次
        n = str(abs(int(value)))
        pat = (r"[（(]\s*" + n + r"\s*[）)]" if value < 0
               else r"(?<![\d.])" + n + r"(?![\d.])")
        if any(re.search(pat, s) for s in snippets):
            return True
    return value == 0 and any(
        re.search(r"(?<![\d.])[-—‐](?![\d.])", s) for s in snippets)


def _normalize_rows(path, rows, global_unit=None, page_texts=None):
    """归一科目名与单位，返回 (合并 items, 母公司 items, 丢弃数)。

    items 每项存 {v, a, unit, statement, listed}：
      - 金额统一换算成「元」，unit 记 "元"；
      - 百分数保留披露的百分点原值（15.31 就是 15.31%），unit 记 "%"；
      - 每股收益保留原值，unit 记 "元/股"；
      - 其他单位（股、件等）也保留原值与单位，不再静默丢弃。
    global_unit 为报表声明单位（机械解析），纯金额行以它为准——LLM 逐页填单位易整体看错。
    page_texts 给出页码→原文时，抄录值不在该页原文里的行直接丢弃，不把看错的数字写进库。"""
    cons, parent, dropped = {}, {}, 0
    items_dict = fin_calc.load_items()
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
        if page_texts is not None and not r.get("_fallback") \
                and not _value_on_page(page_texts.get(r["page"], ""), r["item"], raw_v, unit):
            dropped += 1
            print(f"  [核对] 丢弃第{r['page']}页 {r['item']} = {raw_v:g} {unit}：该页原文找不到这个数")
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
        if page_texts is not None and r.get("statement") == "所有者权益变动表" \
                and name in ("提取盈余公积", "对所有者或股东的分配") and v > 0 \
                and _value_on_page(page_texts.get(r["page"], ""), r["item"], -raw_v, unit):
            # 权益变动行在未分配利润列是减项，模型偶尔抄成盈余公积列的正数；
            # 同页原行里核到对应负数就翻回，R3 的代数口径与入库值才一致
            v = -v
        bucket = parent if r["scope"] == "母公司" else cons
        cls = (items_dict.get(name) or {}).get("s")
        cls_ok = not cls or _STMT_CLASS.get(r["statement"]) == cls
        entry = {"v": v, "a": anchor_of(path, r["page"]), "statement": r["statement"], "listed": listed,
                 "unit": stored_unit, "_cls_ok": cls_ok}
        if name in bucket:
            old = bucket[name]
            if old.get("_cls_ok") is False and cls_ok:
                bucket[name] = entry  # 报表归属与字典一致的行优先，避免现金流量表调整行冒充资产负债表科目
                continue
            if old.get("_cls_ok") and not cls_ok:
                continue
            # 三季报「主要会计数据」同时列本报告期与年初至报告期末，同名的正表行是累计口径，优先保留
            primary = {"资产负债表", "利润表", "现金流量表"}
            if r.get("statement") in primary and old.get("statement") not in primary:
                bucket[name] = entry
                continue
            if old.get("statement") in primary and r.get("statement") not in primary:
                continue
            if old["v"] and v and not 0.1 <= abs(v / old["v"]) <= 10:
                print(f"  [警告] 同名科目量级冲突：{name} 已存 {old['v']:,.0f}，新值 {v:,.0f}（第{r['page']}页），保留先入值")
        else:
            bucket[name] = entry
    for bucket in (cons, parent):
        for spec in bucket.values():
            spec.pop("_cls_ok", None)
    return cons, parent, dropped


# 港股报表把费用与负债总额列成括号减项，A 股列正数；统一翻正后再走勾稽与指标
_HK_EXPENSE_ITEMS = ("营业成本", "销售费用", "管理费用", "研发费用", "税金及附加", "所得税费用")
_LIAB_TOTALS = ("负债合计", "流动负债合计", "非流动负债合计")


def _fix_statement_signs(items):
    rev = (items.get("营业收入") or {}).get("v")
    cost = (items.get("营业成本") or {}).get("v")
    hk_style = bool(rev and rev > 0 and cost is not None and cost < 0)
    if hk_style:
        for name in _HK_EXPENSE_ITEMS:
            spec = items.get(name)
            if spec and spec["v"] < 0:
                spec["v"] = -spec["v"]
    ta = (items.get("资产总计") or {}).get("v")
    if ta and ta > 0:
        for name in _LIAB_TOTALS:
            spec = items.get(name)
            if spec and spec["v"] < 0:
                spec["v"] = -spec["v"]
    return hk_style


def _drop_stale_lines(body, title, page_path, pk, file_name):
    """同一份财报重摄时，清掉该报告期由它派生的旧行，避免名称归一或符号修正后留下重复行。"""
    sections = wiki.body_to_sections(body)
    dropped = 0
    for s, lines in sections.items():
        kept = []
        for ln in lines:
            stale = (ln.startswith(f"【数据】{pk} ") or ln.startswith(f"【指标】{pk} ")
                     or ln.startswith("【管理层】")) and file_name in ln
            if stale:
                dropped += 1
                continue
            kept.append(ln)
        sections[s] = kept
    return wiki.render_page(title, sections, _dir_type(page_path)), dropped


def _fmt_value(v):
    if abs(v - round(v)) < 1e-9:
        return f"{v:,.0f}"
    # 大额金额只留 2 位：千元换算成元后 51,203,109,216.36 会带出 .360001 的浮点尾数；
    # 小额（三季报每股收益常给 3 位小数，如 0.493）按原文保留最多 6 位
    return (f"{v:,.2f}" if abs(v) >= 100 else f"{v:,.6f}").rstrip("0").rstrip(".")


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


def _code6(s):
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", str(s or ""))
    return m.group(1) if m else ""


def _resolve_company_page(company, code):
    """精确/股票代码/别名/包含四级匹配现有公司页。返回 page dict 或 None。"""
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
    # 财报里给的是法定全称（浙江三花智能控制股份有限公司）与裸代码（002050），
    # 已有页多用简称与带市场后缀的别名（002050.SZ），按 6 位代码认同一家公司
    code6 = _code6(code)
    if code6:
        for p in pages:
            if p["type"] != "company":
                continue
            if any(_code6(a) == code6 for a in
                   [p["meta"].get("title")] + list(p["meta"].get("aliases") or [])):
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


def ingest_filing(path, units, unit_kind, manifest, sha, targets=None, task_id=None):
    """财报摄入主流程。返回 ingest_one 形状的结果 dict；勾稽失败返回 None（挂起）。"""
    targets = normalize_targets(targets)
    tasks.check_cancel(task_id)
    meta = _locate(path, units, targets)
    print(f"  [财报] {meta['company']} {meta['pk']}（{meta['period_end']}）审计意见：{meta['audit_opinion']}")
    if _detect_currency(units, meta["sections"]) == "HKD":
        raise UnsupportedFiling("报表以港元列示，不套 A 股四表流程，改按文本材料编译")
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
    page_texts = {u["n"]: u["text"] for u in units}
    cons, parent, dropped = _normalize_rows(path, rows, global_unit, page_texts)
    want = ("资产总计", "负债合计", "所有者权益合计", "营业收入", "净利润")
    missing = [n for n in want if n not in cons]
    key_pages = meta["sections"].get("key") or []
    if missing and key_pages:
        print(f"  [补录] 主要会计数据缺 {'、'.join(missing)}，重抄关键页")
        extra_cons, extra_parent, _ = _normalize_rows(
            path, _transcribe_batch(path, units, key_pages, True), global_unit, page_texts)
        for name, spec in extra_cons.items():
            cons.setdefault(name, spec)
        for name, spec in extra_parent.items():
            parent.setdefault(name, spec)
        left = [n for n in want if n not in cons]
        print(f"  [补录] 补齐 {len(missing) - len(left)} 项" + (f"，仍缺 {'、'.join(left)}" if left else ""))
    left = [n for n in want if n not in cons]
    if left:
        # 抄列可能整批少返回行（LLM 非确定性），勾稽对残缺数据会空转通过；
        # 核心科目补录后仍缺说明抄录不完整，宁挂起重试也不把残缺行写进 wiki
        print(f"  [挂起] 核心科目 {'、'.join(left)} 抄录不齐，整份财报不入库，请重试")
        return None
    hk_style = _fix_statement_signs(cons)
    _fix_statement_signs(parent)
    print(f"  [抄录] 合并 {len(cons)} 项、母公司 {len(parent)} 项，丢弃 {dropped} 行")
    if not cons:
        print("  [拒] 未抄录到任何合并报表科目，不摄入")
        return None

    tol = config.CFG["ingest"].get("filing_check_tol", 0.005)
    supp_ni = _supp_net_profit(units, meta, global_unit)
    if supp_ni is not None:
        print(f"  [R4] 补充资料起点净利润 {supp_ni:,.0f} 元")
    elif meta["sections"].get("cf_supp"):
        print("  [R4] 补充资料未取到起点净利润，跳过")
    if hk_style:
        print("  [勾稽] 港股权益表口径与 A 股不同，R3 跳过")
    fails = fin_calc.check_reconciliations({k: v["v"] for k, v in cons.items()}, tol, supp_ni,
                                           skip=("R3",) if hk_style else ())
    if fails:
        for x in fails:
            print(f"  [勾稽失败] {x}")
        print("  [挂起] 整份财报数值不入库，请人工核对后重试")
        return None

    tasks.check_cancel(task_id)
    quotes = _quotes(path, units, meta, task_id=task_id)
    trows = _target_rows(path, units, meta, targets)
    trows, tdup = _drop_target_dupes(trows, cons)
    if tdup:
        print(f"  [目标] 与四表重复 {tdup} 条，已略")

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
        "targets": trows,
    }
    ind, anc, missing = fin_calc.compute_indicators(periods, pk)
    periods[pk]["indicators"] = {k: {"v": v, "a": anc.get(k) or []} for k, v in ind.items()}
    print(f"  [指标] 算出 {len(ind)} 个，缺失 {len(missing)} 个（缺上年数据或科目）")

    data_lines, ind_lines, quote_lines = _render_lines(path, pk, cons, ind, anc, quotes)
    lines = data_lines + ind_lines + quote_lines + _render_target_lines(pk, trows)
    topic = {
        "title": meta["company"],
        "type": "company",
        "aliases": [meta["code"]] if meta["code"] else [],
        "summary": f"{meta['company']}财报结构化数据（{pk}）",
        "sections": {"经营与财务": lines},
    }
    if hit:
        body, stale = _drop_stale_lines(hit["body"], hit["meta"].get("title") or hit["slug"],
                                         hit["path"], pk, path.name)
        if stale:
            print(f"  [替换] 清掉同报告期旧行 {stale} 条")
        _apply_merge(hit["path"], hit["meta"], body, topic, [rel], f"财报摄入 {path.name}")
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
    if targets:
        extra.append(f"研报目标数据：申请 {len(targets)} 项，抄到 {len(trows)} 条"
                     + (f"，与四表重复 {tdup} 条已略" if tdup else ""))
    unlisted = [k for k, v in cons.items() if not v.get("listed")]
    if unlisted:
        extra.append(f"字典外科目（保留原名）：{'、'.join(unlisted[:12])}{' 等' if len(unlisted) > 12 else ''}")
    if missing:
        extra.append(f"缺失指标（缺科目或上年数据）：{'、'.join(missing[:15])}{' 等' if len(missing) > 15 else ''}")
    _write_source_page(path, touched, meta["period_end"], len(lines), extra)

    entry = {
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
    }
    if targets:
        entry["targets"] = [t["item"] for t in targets]
    manifest["files"].append(entry)
    # 调试转储
    dump = config.EXTRACTS_DIR / (path.stem + ".filing.json")
    dump.write_text(json.dumps({
        "meta": meta, "rows_raw": rows, "cons": cons, "parent": parent,
        "fails": fails, "supp_net_profit": supp_ni, "indicators": periods[pk]["indicators"],
        "missing": missing, "quotes": quotes, "targets_requested": targets, "target_rows": trows,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    log = []
    if created:
        log.append("新建 " + "、".join(created) + f"（财报 {pk}）")
    else:
        log.append(f"并入 {touched[0]}（财报 {pk}，+{len(lines)} 行）")
    log.append(f"指标 {len(ind)} 个、缺失 {len(missing)} 个")
    if targets:
        log.append(f"目标数据 {len(trows)} 条")
    return {"file": path.name, "entries_count": len(lines), "log": "；".join(log),
            "touched": touched}
