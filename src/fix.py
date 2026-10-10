from . import config, wiki
from .audit import _norm_text
from .ingest import _apply_merge, ingest_one, load_manifest, save_manifest

# 按审校报告执行修复。删除条目类操作出自人工决策（向导勾选），
# 明确绕过「锚点只增不减」断言，直接改正文并在 history 与操作日志留痕。


def _page_by_slug(slug):
    for p in wiki.load_pages():
        if p["slug"] == slug:
            return p
    return None


def _fix_ingest(action, notes):
    rel = action.get("path") or ""
    p = (config.ROOT / rel).resolve()
    if config.RAW_DIR.resolve() not in p.parents or not p.exists():
        notes.append(f"跳过摄入：{rel} 不在 raw/ 中")
        return
    manifest = load_manifest()
    r = ingest_one(p, manifest)
    save_manifest(manifest)
    wiki.rebuild_index_md(wiki.load_pages())
    if r:
        wiki.append_log("fix", f"按审校摄入 {r['file']}（{r['entries_count']} 条）")
        notes.append(f"已摄入 {r['file']}：{r['log']}")
    else:
        notes.append(f"{rel} 未产出条目（可能已摄入或被拒）")


def _fix_merge(action, notes):
    a = _page_by_slug(action.get("from") or "")
    b = _page_by_slug(action.get("into") or "")
    if not a or not b or a["path"] == b["path"]:
        notes.append("跳过合并：页面不存在或为同一页")
        return
    title = str(a["meta"].get("title") or a["slug"])
    topic = {
        "title": title,
        "type": a["type"],
        "aliases": [str(x) for x in (a["meta"].get("aliases") or [])],
        "summary": str(a["meta"].get("summary") or ""),
        "sections": wiki.body_to_sections(a["body"]),
    }
    extra = [s for s in (a["meta"].get("sources") or []) if s not in (b["meta"].get("sources") or [])]
    added = _apply_merge(b["path"], b["meta"], b["body"], topic, extra, f"审校合并：并入 {title}")
    a["path"].unlink()
    wiki.rebuild_index_md(wiki.load_pages())
    wiki.append_log("fix", f"按审校合并：{title} 并入 {b['slug']}（+{added} 条），原页删除")
    notes.append(f"已合并 {title} → {b['slug']}（+{added} 条）")


def _fix_mark_reviewed(action, notes):
    p = _page_by_slug(action.get("page") or "")
    if not p:
        notes.append("跳过标注：页面不存在")
        return
    meta = p["meta"]
    hist = meta.get("history") or []
    hist.append(f"{config.today_str()} 已人工复核（审校）")
    meta["history"] = hist
    wiki.write_page(p["path"], meta, p["body"])
    notes.append(f"已在 {p['slug']} 记录人工复核")


def _fix_conflict(action, notes):
    p = _page_by_slug(action.get("page") or "")
    if not p:
        notes.append("跳过矛盾处理：页面不存在")
        return
    keep = action.get("keep")
    qa, qb = action.get("quote_a") or "", action.get("quote_b") or ""

    def match(line, quote):
        return bool(quote) and _norm_text(quote) in _norm_text(line)

    removed = 0
    if keep in ("a", "b"):
        drop_q = qb if keep == "a" else qa
        new_lines = []
        for ln in p["body"].splitlines():
            if ln.strip().startswith("- ") and match(ln, drop_q):
                removed += 1
                continue
            new_lines.append(ln)
        body = "\n".join(new_lines).rstrip() + "\n"
        note = f"{config.today_str()} 矛盾处理：保留{'A' if keep == 'a' else 'B'}方，删除 {removed} 条（人工决策）"
    else:
        body = p["body"]
        note = f"{config.today_str()} 矛盾处理：双方均保留（人工决策）"
    meta = p["meta"]
    hist = meta.get("history") or []
    hist.append(note)
    meta["history"] = hist
    wiki.write_page(p["path"], meta, body)
    wiki.append_log("fix", f"{p['slug']} {note}")
    notes.append(f"{p['slug']}：{note}")


def apply_fixes(actions, task_id=None):
    notes = []
    actions = [a for a in actions if isinstance(a, dict)]
    for i, action in enumerate(actions, 1):
        if task_id:
            from . import tasks
            tasks.update(task_id, current=i, detail=action.get("type") or "")
        t = action.get("type")
        try:
            if t == "ingest":
                _fix_ingest(action, notes)
            elif t == "merge":
                _fix_merge(action, notes)
            elif t == "mark_reviewed":
                _fix_mark_reviewed(action, notes)
            elif t == "conflict":
                _fix_conflict(action, notes)
            else:
                notes.append(f"忽略未知动作 {t}")
        except Exception as e:
            notes.append(f"{t} 执行失败：{e}")
    wiki.append_log("fix", "按审校报告修复完成：" + "；".join(notes[:5]))
    return notes
