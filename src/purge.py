"""按原文件删除：原文件、提取缓存、财报结构化数据、来源页，以及 wiki 里引用它的知识条目。

删除走后台任务（tasks 的 delete 类），与摄入等任务并行互不排队；清单落盘在 ingest.MANIFEST_LOCK
内完成，且只做「删条目」这一种改动，不与摄入线的追加相互覆盖。
"""

import json
from pathlib import Path

from . import config, tasks, wiki
from .ingest import MANIFEST_LOCK, load_manifest, save_manifest
from .pdf_extract import raw_file_index

KIND_LABELS = {"report": "研报", "filing": "财报", "policy": "政策文件"}
CACHE_SUFFIXES = (".units.json", ".entries.json", ".filing.json")


def _cites(line, name):
    return any(a[0] == name for a in wiki.ANCHOR_RE.findall(line))


def _impact(pages):
    """文件名 → {statements: 被引用的条目数, pages: 涉及的页面标题, empty_pages: 删空后会被移除的页面}。

    empty_pages 的判据与 purge_one 一致：剩下的条目要么引用了别的文件（页面留存），
    要么一条锚点都没有（无锚点条目跟这份材料一起走，页面被移除）。
    """
    out = {}
    for p in pages:
        if p["type"] == "source":
            continue
        title = str(p["meta"].get("title") or p["slug"])
        per = {}
        orphan = 0
        for ln in p["body"].splitlines():
            if not ln.strip().startswith("- "):
                continue
            anchors = wiki.ANCHOR_RE.findall(ln)
            if not anchors:
                orphan += 1
                continue
            for f in {a[0] for a in anchors}:
                per[f] = per.get(f, 0) + 1
        for f, n in per.items():
            e = out.setdefault(f, {"statements": 0, "pages": [], "empty_pages": []})
            e["statements"] += n
            e["pages"].append(title)
            if n + orphan == p["statements"]:
                e["empty_pages"].append(title)
    return out


def _row(name, rel, entry, imp, missing):
    entry = entry or {}
    return {
        "name": name,
        "path": rel,
        "kind": entry.get("source_type") or "",
        "kind_label": KIND_LABELS.get(entry.get("source_type"), "未摄入" if not entry else "—"),
        "report_date": entry.get("report_date") or "",
        "entries_count": entry.get("entries_count") or 0,
        "ingested_at": entry.get("ingested_at") or "",
        "statements": imp.get("statements", 0),
        "pages": imp.get("pages", []),
        "empty_pages": imp.get("empty_pages", []),
        "missing": missing,
    }


def file_entries():
    """原文件页清单：raw 里已改名的文件，加上清单里有、raw 里已不在的残留（供清掉）。"""
    by_name = {f["file"]: f for f in load_manifest()["files"]}
    impact = _impact(wiki.load_pages())
    rows = []
    for name, path in raw_file_index().items():
        if path.parent == config.INBOX_DIR:  # 还在收件台、没归类的，不算入库
            continue
        rows.append(_row(name, path.relative_to(config.ROOT).as_posix(),
                         by_name.get(name), impact.get(name) or {}, missing=False))
    listed = {r["name"] for r in rows}
    for name, f in by_name.items():
        if name not in listed:
            rows.append(_row(name, f.get("path") or "", f, impact.get(name) or {}, missing=True))
    fresh = sorted((r for r in rows if not r["ingested_at"]), key=lambda r: r["name"])
    old = sorted((r for r in rows if r["ingested_at"]), key=lambda r: r["ingested_at"], reverse=True)
    return fresh + old


def _source_pages(pages, rel, stem):
    """这份材料写出来的来源页：front matter 的 sources 记着它的相对路径，或页名就是它的文件名。"""
    out = []
    for p in pages:
        if p["type"] != "source":
            continue
        srcs = [str(s) for s in (p["meta"].get("sources") or [])]
        if (rel and rel in srcs) or p["slug"] == stem:
            out.append(p)
    return out


def _drop_page(p, title, stats, removed, why):
    p["path"].unlink()
    stats["statements"] += removed
    stats["pages_deleted"].append(title)
    stats["removed"].append(f"页面 {title}（-{removed} 条，{why}）")


def _prune_empty_dirs(d):
    stops = {config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR, config.RAW_DIR}
    while d not in stops and d.is_dir() and not any(d.iterdir()):
        d.rmdir()
        d = d.parent


def _drop_fin_periods(rel, stats):
    """财报文件对应的结构化数据：删掉它的报告期；一份不剩就删掉整个公司文件。"""
    if not rel:
        return
    for f in sorted(config.FIN_DIR.glob("*.json")):
        store = json.loads(f.read_text(encoding="utf-8"))
        periods = store.get("periods") or {}
        drop = [k for k, v in periods.items() if str(v.get("source") or "") == rel]
        if not drop:
            continue
        for k in drop:
            periods.pop(k)
        slug = store.get("slug") or f.stem
        if periods:
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(f)
            stats["removed"].append(f"{slug} 财报期 {'、'.join(drop)}")
        else:
            f.unlink()
            stats["removed"].append(f"{slug} 财报数据（{'、'.join(drop)}，无剩余期数）")


def purge_one(name, manifest):
    """删一份原文件及其全部关联数据。调用方负责在 MANIFEST_LOCK 内落盘清单。"""
    stats = {"file": name, "statements": 0, "pages_deleted": [], "removed": []}
    path = raw_file_index().get(name)
    entry = next((f for f in manifest["files"] if f["file"] == name), None)
    rel = path.relative_to(config.ROOT).as_posix() if path else ((entry or {}).get("path") or "")
    stem = Path(name).stem

    # 1. 主题页正文：摘掉引用该文件的条目，一条不剩的页面整页删除
    pages = wiki.load_pages()
    victims = {p["path"] for p in _source_pages(pages, rel, stem)}
    for p in pages:
        if p["path"] in victims:
            continue
        sections = wiki.body_to_sections(p["body"])
        kept = {s: [ln for ln in lns if not _cites(ln, name)] for s, lns in sections.items()}
        removed = sum(len(v) for v in sections.values()) - sum(len(v) for v in kept.values())
        if not removed:
            continue
        title = str(p["meta"].get("title") or p["slug"])
        if not any(kept.values()):
            _drop_page(p, title, stats, removed, "已空")
            continue
        body = wiki.render_page(title, kept, p["type"])
        meta = dict(p["meta"])
        srcs = [s for s in (meta.get("sources") or []) if str(s) != rel]
        if not srcs:  # 来源列表被删空时，按正文剩下的锚点回填
            srcs = sorted({a[0] for a in wiki.anchors_of(body)})
        if not srcs:
            # 剩下的条目一条锚点都没有（财报里个别派生指标行会这样，见 tests/test_purge.py），
            # 无法归属到别的文件，同样是这份材料的产物，整页移除
            _drop_page(p, title, stats, removed + sum(len(v) for v in kept.values()), "剩下的条目无锚点")
            continue
        stats["statements"] += removed
        meta["sources"] = srcs
        meta["updated"] = config.today_str()
        meta["history"] = list(meta.get("history") or []) + [f"{config.today_str()} 删除 {name}（-{removed} 条）"]
        wiki.write_page(p["path"], meta, body)
        stats["removed"].append(f"页面 {title}（-{removed} 条）")

    # 2. 来源页
    for p in _source_pages(pages, rel, stem):
        p["path"].unlink()
        stats["removed"].append(f"来源页 {p['slug']}")

    # 3. 财报结构化数据与提取缓存
    _drop_fin_periods(rel, stats)
    for suf in CACHE_SUFFIXES:
        f = config.EXTRACTS_DIR / (stem + suf)
        if f.exists():
            f.unlink()

    # 4. 原文件本体（顺手清掉空的行业/公司子目录）
    if path and path.exists():
        path.unlink()
        _prune_empty_dirs(path.parent)

    # 5. 清单条目
    manifest["files"] = [f for f in manifest["files"] if f["file"] != name]
    return stats


def _drop_dangling_links(results):
    """被删掉的页面可能还被别的页面 [[双链]] 引用，把双链降级成纯文字，校验才不报错。"""
    gone = {t for r in results for t in (r.get("pages_deleted") or [])}
    if not gone:
        return
    for p in wiki.load_pages():
        hit = gone & wiki.wikilinks_of(p["body"])
        if not hit:
            continue
        body = p["body"]
        for t in hit:
            body = body.replace(f"[[{t}]]", t)
        meta = dict(p["meta"])
        meta["updated"] = config.today_str()
        meta["history"] = list(meta.get("history") or []) + [
            f"{config.today_str()} 移除指向已删页面 {'、'.join(sorted(hit))} 的双链"]
        wiki.write_page(p["path"], meta, body)


def purge_files(names, task_id=None):
    results = []
    for i, name in enumerate(names, 1):
        if task_id:
            tasks.update(task_id, current=i - 1, detail=f"第 {i}/{len(names)} 份：{name} 正在清理")
        try:
            with MANIFEST_LOCK:
                manifest = load_manifest()
                r = purge_one(name, manifest)
                save_manifest(manifest)
        except Exception as e:
            r = {"file": name, "error": str(e)}
        results.append(r)
        if task_id:
            tasks.update(task_id, current=i)
    _drop_dangling_links(results)
    wiki.rebuild_index_md(wiki.load_pages())
    ok = [r for r in results if "error" not in r]
    if ok:
        wiki.append_log("delete", "；".join(
            f"{r['file']}（-{r['statements']} 条，移除页面 {'、'.join(r['pages_deleted']) or '无'}）" for r in ok))
    return results
