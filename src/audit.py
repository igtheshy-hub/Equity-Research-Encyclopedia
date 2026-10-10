import re
from datetime import date, datetime

from . import config, llm, wiki
from .ingest import iter_pdfs, load_manifest

CONFLICT_PROMPT = """你是知识库审校员。下面是 wiki 页面「{TITLE}」的全部条目。找出页内互相冲突的陈述对。

规则：
- 只报告页面里确有的冲突，摘录必须与原文逐字一致（不含句尾锚点部分）
- 区分两类：「矛盾」= 同一事实的不同说法（如相反评级、同一数据打架）；「时点差异」= 不同日期报告对同一指标的不同预测（属正常更新，不是错误）
- 每对冲突给出双方摘录、各自句尾的锚点（原样复制 [[文件.pdf#pN]]）、一句话说明
- 没有冲突就给空数组

只输出 JSON：
{"conflicts": [{"kind": "矛盾 或 时点差异", "quote_a": "摘录", "anchor_a": "[[文件.pdf#pN]]", "quote_b": "摘录", "anchor_b": "[[文件.pdf#pN]]", "note": "说明"}]}

--- 页面条目开始 ---
{BODY}
--- 页面条目结束 ---"""

GAP_PROMPT = """你是投研审校员。wiki 页面「{TITLE}」当前条目如下。列出 3 到 5 个投研该主题时常用、但页面目前没有覆盖的信息点；页面已覆盖的不要再列。

只输出 JSON：
{"gaps": [{"point": "信息点", "why": "一句话为什么投研常用"}]}

--- 页面条目开始 ---
{BODY}
--- 页面条目结束 ---"""


def _norm_text(s):
    return re.sub(r"[\s，。、；：''\"\"（）()【】\\[\\]]+", "", str(s))


def _parse_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _anchor_of(s, page):
    m = wiki.ANCHOR_RE.search(str(s or ""))
    if not m:
        return None
    pair = (m.group(1), int(m.group(2)))
    return pair if pair in page["anchors"] else None


def _quote_in_page(q, page):
    body_no_anchor = wiki.ANCHOR_RE.sub("", page["body"])
    return _norm_text(q) in _norm_text(body_no_anchor)


def _llm_page(task_prompt, page, key):
    lines = [ln for ln in page["body"].splitlines() if ln.strip().startswith("- ")]
    if not lines:
        return []
    content = task_prompt.replace("{TITLE}", str(page["meta"].get("title") or page["slug"])).replace("{BODY}", "\n".join(lines))
    try:
        out = llm.chat([{"role": "user", "content": content}], task="audit", response_json=True)
    except llm.LLMError:
        return []
    return out.get(key) or []


def _conflicts_of(page):
    lines = [ln for ln in page["body"].splitlines() if ln.strip().startswith("- ")]
    if len(lines) < 2:
        return []
    res = []
    for c in _llm_page(CONFLICT_PROMPT, page, "conflicts"):
        if not isinstance(c, dict):
            continue
        qa, qb = c.get("quote_a"), c.get("quote_b")
        if not qa or not qb:
            continue
        if not _quote_in_page(qa, page) or not _quote_in_page(qb, page):
            continue
        if _anchor_of(c.get("anchor_a"), page) is None or _anchor_of(c.get("anchor_b"), page) is None:
            continue
        res.append(c)
    return res


def _gaps_of(page):
    return [g for g in _llm_page(GAP_PROMPT, page, "gaps") if isinstance(g, dict) and g.get("point")]


def run():
    pages = wiki.load_pages()
    manifest = load_manifest()
    today = date.today()
    stale_days = config.CFG["audit"]["stale_days"]

    conflicts, time_diffs, gaps_by_page = [], [], []
    for p in pages:
        if p["type"] == "source":
            continue
        for c in _conflicts_of(p):
            c["page"] = str(p["meta"].get("title") or p["slug"])
            (conflicts if c.get("kind") == "矛盾" else time_diffs).append(c)
        g = _gaps_of(p)
        if g:
            gaps_by_page.append({"page": str(p["meta"].get("title") or p["slug"]), "gaps": g})

    stale = []
    for p in pages:
        d = _parse_date(p["meta"].get("updated"))
        if d is None or (today - d).days > stale_days:
            stale.append(str(p["meta"].get("title") or p["slug"]))

    known = {f["file"] for f in manifest["files"]}
    missing_files = [p for p in iter_pdfs() if p.name not in known]

    non_source = [p for p in pages if p["type"] != "source"]

    targets = {}
    for p in pages:
        targets[str(p["meta"].get("title") or p["slug"])] = p
        targets[p["slug"]] = p
    inlinks = {p["slug"]: [] for p in pages}
    for p in pages:
        for link in wiki.wikilinks_of(p["body"]):
            tgt = targets.get(link)
            if tgt is not None and tgt["slug"] != p["slug"]:
                inlinks[tgt["slug"]].append(str(p["meta"].get("title") or p["slug"]))
    orphans = [str(p["meta"].get("title") or p["slug"]) for p in non_source if not inlinks[p["slug"]]]

    merge_suggestions = []
    for i, a in enumerate(non_source):
        for b in non_source[i + 1:]:
            if a["type"] != b["type"]:
                continue
            ta = str(a["meta"].get("title") or a["slug"])
            tb = str(b["meta"].get("title") or b["slug"])
            if ta != tb and (ta in tb or tb in ta):
                merge_suggestions.append(f"{ta} 与 {tb} 标题互为包含，疑似应合并")
    for p in non_source:
        if p["statements"] < config.CFG["audit"]["orphan_min_statements"] and len(p["meta"].get("sources") or []) <= 1:
            merge_suggestions.append(
                f"{p['meta'].get('title') or p['slug']} 仅 {p['statements']} 条、单一来源，疑似碎片页"
            )

    out_lines = [f"# 审校报告 {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    out_lines += [f"范围：wiki/ 共 {len(pages)} 页（含来源摘要页）", ""]

    out_lines += [f"## 矛盾（{len(conflicts)}）"]
    if conflicts:
        for c in conflicts:
            out_lines += [
                f"### [{c['page']}] {c.get('note', '')}",
                f"- A：「{c['quote_a']}」{c['anchor_a']}",
                f"- B：「{c['quote_b']}」{c['anchor_b']}",
                "",
            ]
    else:
        out_lines += ["（无）", ""]
    out_lines += [f"### 时点差异（{len(time_diffs)}，属正常更新，不报警）"]
    if time_diffs:
        for c in time_diffs:
            out_lines += [f"- [{c['page']}] {c.get('note', '')}：A「{c['quote_a']}」{c['anchor_a']} vs B「{c['quote_b']}」{c['anchor_b']}"]
    else:
        out_lines += ["（无）"]
    out_lines.append("")

    out_lines += [f"## 过期（{len(stale)}）"]
    out_lines += [f"- {s}" for s in stale] or ["（无）"]
    out_lines.append("")

    out_lines += [f"## 未摄入（文件 {len(missing_files)}，内容缺口 {len(gaps_by_page)} 页）"]
    for f in missing_files:
        out_lines += [f"- raw/ 中存在但未摄入：{f.relative_to(config.ROOT).as_posix()}"]
    for g in gaps_by_page:
        out_lines += [f"- [{g['page']}] 内容缺口："]
        out_lines += [f"  - {x['point']}（{x.get('why', '')}）" for x in g["gaps"]]
    if not missing_files and not gaps_by_page:
        out_lines += ["（无）"]
    out_lines.append("")

    out_lines += [f"## 孤立（{len(orphans)}）"]
    out_lines += [f"- {s}（无任何页面引用）" for s in orphans] or ["（无）"]
    for m in merge_suggestions:
        out_lines += [f"- {m}"]
    out_lines.append("")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = config.OUTPUTS_DIR / f"audit-{ts}.md"
    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    wiki.append_log(
        "audit",
        f"审校报告 {out_path.name}：矛盾 {len(conflicts)}、过期 {len(stale)}、"
        f"未摄入 {len(missing_files)} 文件/{len(gaps_by_page)} 页、孤立 {len(orphans)}",
    )
    return {
        "path": out_path,
        "counts": {
            "矛盾": len(conflicts),
            "时点差异": len(time_diffs),
            "过期": len(stale),
            "未摄入文件": len(missing_files),
            "内容缺口页": len(gaps_by_page),
            "孤立": len(orphans),
        },
    }
