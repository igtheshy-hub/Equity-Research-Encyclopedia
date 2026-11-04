import re
from datetime import date, datetime

from . import config, llm, source_priority, wiki
from .classify import sha256_of
from .ingest import iter_pdfs, load_manifest

CONFLICT_PROMPT = """你是知识库审校员。下面是 wiki 页面「{TITLE}」的全部条目。找出页内互相冲突的陈述对。

规则：
- 只报告页面里确有的冲突，摘录必须与原文逐字一致（不含句尾锚点部分）
- 区分两类：「矛盾」= 同一口径、同一时点的同一事实出现互相排斥的数值或说法；「时点差异」= 不同日期报告对同一指标的不同读数或预测（属正常更新，不是错误）
- 以下情况不是矛盾，不要报告：单季与累计/年化口径不同；不同日期的周度行情、估值、原材料价格；不同机构的评级标准或基准指数不同；货币资金与现金及现金等价物、基本 EPS 与稀释 EPS、不同计算口径的衍生指标；名称不同但数值一致
- 公司财务数据的来源优先级：财报（年报、半年报、季报）> 公告/招股书 > 券商研报 > 其他材料。同一公司同一口径的财务指标，财报与研报数字不同，按「矛盾」报告，以最新报告期的财报为准；单季对累计的口径差异按「时点差异」处理并写明口径
- 每对冲突给出双方摘录、各自句尾的锚点（原样复制 [[文件.pdf#pN]]）、一句话说明
- 没有冲突就给空数组

只输出 JSON：
{"conflicts": [{"kind": "矛盾 或 时点差异", "quote_a": "摘录", "anchor_a": "[[文件.pdf#pN]]", "quote_b": "摘录", "anchor_b": "[[文件.pdf#pN]]", "note": "说明"}]}

--- 页面条目开始 ---
{BODY}
--- 页面条目结束 ---"""

GAP_PROMPT = """你是行业知识库审校员。wiki 页面「{TITLE}」当前条目如下。列出 3 到 5 个研究该主题时常用、但页面目前没有覆盖的信息点；页面已覆盖的不要再列。

只输出 JSON：
{"gaps": [{"point": "信息点", "why": "一句话为什么研究时常用"}]}

--- 页面条目开始 ---
{BODY}
--- 页面条目结束 ---"""


_SELF_NEGATING = re.compile(
    r"不构成(?:实质|直接|数值|数据)?矛盾|无矛盾|并非矛盾|不属于矛盾|"
    r"不构成(?:数据|实质|数值)?冲突|属于正常(?:差异|更新)|信息详略|不同财务指标"
)


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
    pair = (m.group(1), m.group(2))
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


def _conflicts_of(page, manifest=None):
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
        # 模型常把「相关但不是冲突」的配对也返回，并在 note 里自认不矛盾；这类噪声不进报告
        if _SELF_NEGATING.search(str(c.get("note") or "")):
            continue
        c.update(source_priority.conflict_note(c.get("anchor_a"), c.get("anchor_b"), manifest))
        res.append(c)
    return res


def _gaps_of(page):
    return [g for g in _llm_page(GAP_PROMPT, page, "gaps") if isinstance(g, dict) and g.get("point")]


def audit_raw_files(manifest):
    """按 SHA256 区分真未摄入、重复下载件、同名但内容变化的文件。"""
    by_path, by_sha = {}, {}
    for f in manifest["files"]:
        if f.get("path"):
            by_path[f["path"]] = f
        if f.get("sha256"):
            by_sha.setdefault(f["sha256"], f)
    missing, duplicates, changed = [], [], []
    for p in iter_pdfs():
        rel = p.relative_to(config.ROOT).as_posix()
        sha = sha256_of(p)
        entry = by_path.get(rel)
        if entry is not None:
            if entry.get("sha256") == sha:
                continue
            changed.append({"path": rel, "manifest_sha256": entry.get("sha256"), "sha256": sha})
            continue
        same = by_sha.get(sha)
        if same is not None:
            duplicates.append({"path": rel, "duplicate_of": same.get("path") or same["file"]})
            continue
        missing.append(rel)
    return missing, duplicates, changed


def run(task_id=None):
    pages = wiki.load_pages()
    manifest = load_manifest()
    today = date.today()
    stale_days = config.CFG["audit"]["stale_days"]

    def tick(i, n, name):
        if task_id:
            from . import tasks
            tasks.update(task_id, current=i, detail=f"已检查 {i}/{n} 页：{name}")

    conflicts, time_diffs, gaps_by_page = [], [], []
    check_pages = [p for p in pages if p["type"] != "source"]
    for i, p in enumerate(check_pages, 1):
        tick(i, len(check_pages), str(p["meta"].get("title") or p["slug"]))
        for c in _conflicts_of(p, manifest):
            c["page"] = str(p["meta"].get("title") or p["slug"])
            c["slug"] = p["slug"]
            (conflicts if c.get("kind") == "矛盾" else time_diffs).append(c)
        g = _gaps_of(p)
        if g:
            gaps_by_page.append({"page": str(p["meta"].get("title") or p["slug"]), "gaps": g})

    stale = []
    for p in pages:
        d = _parse_date(p["meta"].get("updated"))
        if d is None or (today - d).days > stale_days:
            stale.append({"slug": p["slug"], "title": str(p["meta"].get("title") or p["slug"])})

    # 观点老化 lint：【观点】行依据报告期早于页内最新【数据】报告期 → 列入过期待复核
    pk_re = re.compile(r"^【(数据|观点)】(\d{4})(FY|H1|Q1|Q3) ")
    pk_order = {"Q1": 0, "H1": 1, "Q3": 2, "FY": 3}
    for p in pages:
        latest = None
        opinions = []
        for ln in p["body"].splitlines():
            m = pk_re.match(ln.strip().removeprefix("- ").strip())
            if not m:
                continue
            key = (int(m.group(2)), pk_order[m.group(3)])
            if m.group(1) == "数据":
                latest = max(latest, key) if latest else key
            else:
                opinions.append((key, ln.strip().removeprefix("- ").strip()))
        if latest is None:
            continue
        for key, ln in opinions:
            if key < latest:
                stale.append({
                    "slug": p["slug"],
                    "title": str(p["meta"].get("title") or p["slug"]),
                    "note": f"观点依据期早于最新数据期，待复核：{ln[:60]}",
                })

    # 未摄入审计按内容而不是文件名判断：同名重复下载件会被 unique_target 改成
    # -2/-3，旧逻辑把它当成新文件；内容相同的副本应单列为「重复件」。
    missing_rel, duplicate_files, changed_files = audit_raw_files(manifest)
    missing_files = [config.ROOT / p for p in missing_rel]

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

    # 结构化合并建议，供网页端修复向导勾选方向
    merge_suggestions = []
    for i, a in enumerate(non_source):
        for b in non_source[i + 1:]:
            if a["type"] != b["type"]:
                continue
            ta = str(a["meta"].get("title") or a["slug"])
            tb = str(b["meta"].get("title") or b["slug"])
            if ta != tb and (ta in tb or tb in ta):
                merge_suggestions.append({
                    "kind": "pair",
                    "a": {"slug": a["slug"], "title": ta},
                    "b": {"slug": b["slug"], "title": tb},
                    "note": "标题互为包含，疑似应合并",
                })
    for p in non_source:
        if p["statements"] < config.CFG["audit"]["orphan_min_statements"] and len(p["meta"].get("sources") or []) <= 1:
            merge_suggestions.append({
                "kind": "fragment",
                "slug": p["slug"],
                "title": str(p["meta"].get("title") or p["slug"]),
                "statements": p["statements"],
                "note": f"仅 {p['statements']} 条、单一来源，疑似碎片页",
            })

    out_lines = [f"# 审校报告 {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    out_lines += [f"范围：wiki/ 共 {len(pages)} 页（含来源摘要页）", ""]
    out_lines += ["口径说明：未摄入与文件重复按 SHA256 比对 manifest，是确定性检查；矛盾与内容缺口由模型逐页判断，供人工复核，不是错误清单。", ""]

    out_lines += [f"## 矛盾（{len(conflicts)}）"]
    if conflicts:
        for c in conflicts:
            out_lines += [
                f"### [{c['page']}] {c.get('note', '')}",
                f"- A：「{c['quote_a']}」{c['anchor_a']}（{c.get('source_a', '')}）",
                f"- B：「{c['quote_b']}」{c['anchor_b']}（{c.get('source_b', '')}）",
                f"- 取舍：{c.get('source_note', '')}",
                "",
            ]
    else:
        out_lines += ["（无）", ""]
    out_lines += [f"### 时点差异（{len(time_diffs)}，属正常更新，不报警）"]
    if time_diffs:
        for c in time_diffs:
            out_lines += [f"- [{c['page']}] {c.get('note', '')}：A「{c['quote_a']}」{c['anchor_a']}"
                          f"（{c.get('source_a', '')}） vs B「{c['quote_b']}」{c['anchor_b']}"
                          f"（{c.get('source_b', '')}）；{c.get('source_note', '')}"]
    else:
        out_lines += ["（无）"]
    out_lines.append("")

    out_lines += [f"## 过期（{len(stale)}）"]
    out_lines += [f"- {s['title']}" + (f"（{s['note']}）" if s.get("note") else "") for s in stale] or ["（无）"]
    out_lines.append("")

    out_lines += [f"## 未摄入（文件 {len(missing_files)}，重复件 {len(duplicate_files)}，同名变更 {len(changed_files)}）"]
    for f in missing_files:
        out_lines += [f"- raw/ 中存在但未摄入：{f.relative_to(config.ROOT).as_posix()}"]
    for d in duplicate_files:
        out_lines += [f"- 重复件（SHA256 与已摄入文件一致，未摄入）：{d['path']} ← {d['duplicate_of']}"]
    for c in changed_files:
        out_lines += [f"- 同名但内容已变化，需重新确认：{c['path']}"]
    if not missing_files and not duplicate_files and not changed_files:
        out_lines += ["（无）"]
    out_lines.append("")

    out_lines += [f"## 内容缺口（{len(gaps_by_page)} 页，模型建议的补全点，不是错误）"]
    for g in gaps_by_page:
        out_lines += [f"- [{g['page']}] 内容缺口："]
        out_lines += [f"  - {x['point']}（{x.get('why', '')}）" for x in g["gaps"]]
    if not gaps_by_page:
        out_lines += ["（无）"]
    out_lines.append("")

    out_lines += [f"## 孤立（{len(orphans)}）"]
    out_lines += [f"- {s}（无任何页面引用）" for s in orphans] or ["（无）"]
    for m in merge_suggestions:
        if m["kind"] == "pair":
            out_lines += [f"- {m['a']['title']} 与 {m['b']['title']} {m['note']}"]
        else:
            out_lines += [f"- {m['title']} {m['note']}"]
    out_lines.append("")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = config.OUTPUTS_DIR / f"audit-{ts}.md"
    config.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_lines), encoding="utf-8")
    # 结构化结果落盘，供网页端「按报告修改 wiki」向导使用
    import json as _json

    data_path = config.DATA_DIR / f"audit-{ts}.json"
    data_path.write_text(_json.dumps({
        "ts": ts,
        "report": out_path.name,
        "counts": {
            "矛盾": len(conflicts), "时点差异": len(time_diffs), "过期": len(stale),
            "未摄入文件": len(missing_files), "重复文件": len(duplicate_files),
            "同名变更": len(changed_files), "内容缺口页": len(gaps_by_page), "孤立": len(orphans),
        },
        "conflicts": conflicts,
        "stale": stale,
        "missing_files": [p.relative_to(config.ROOT).as_posix() for p in missing_files],
        "duplicate_files": duplicate_files,
        "changed_files": changed_files,
        "gaps_by_page": gaps_by_page,
        "orphans": orphans,
        "merge_suggestions": merge_suggestions,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    wiki.append_log(
        "audit",
        f"审校报告 {out_path.name}：矛盾 {len(conflicts)}、过期 {len(stale)}、"
        f"未摄入 {len(missing_files)} 文件/重复 {len(duplicate_files)} 件/同名变更 {len(changed_files)} 件、"
        f"内容缺口 {len(gaps_by_page)} 页、孤立 {len(orphans)}",
    )
    return {
        "path": out_path,
        "counts": {
            "矛盾": len(conflicts),
            "时点差异": len(time_diffs),
            "过期": len(stale),
            "未摄入文件": len(missing_files),
            "重复文件": len(duplicate_files),
            "同名变更": len(changed_files),
            "内容缺口页": len(gaps_by_page),
            "孤立": len(orphans),
        },
    }
