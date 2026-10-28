"""按原文件删除：原文件、提取缓存、财报结构化数据、来源页，以及 wiki 里引用它的知识条目。

两条硬规则：
  1. 一份文件删到一半出错，就回滚这一份并停下整批，绝不留下半套数据还报「完成」。
  2. 引用了别的文件、自己却没有锚点的条目不再当作「孤儿」连带删除；保留下来并在页面
     标 needs_review，提示人工复核。删除只摘掉指向该文件的锚点，不误伤同名的其他文件。
"""

import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from . import config, tasks, wiki
from .ingest import MANIFEST_LOCK, load_manifest, save_manifest
from .pdf_extract import find_raw_file, raw_file_groups

KIND_LABELS = {"report": "研报", "filing": "财报", "policy": "政策文件"}
CACHE_SUFFIXES = (".units.json", ".entries.json", ".filing.json")


class PurgeError(RuntimeError):
    pass


def _cited_files(line):
    return {a[0] for a in wiki.ANCHOR_RE.findall(line)}


def _cites(line, name):
    return name in _cited_files(line)


def _strip_anchor(line, name):
    """只摘掉指向该文件的锚点；同一行还引用别的文件时，其他锚点原样保留。"""
    out = wiki.ANCHOR_RE.sub(lambda m: "" if m.group(1) == name else m.group(0), line)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([，。；、）])", r"\1", out)
    return out.rstrip()


def _impact(pages):
    """文件名 → {statements, pages, empty_pages}。

    statements 统计「只引用这份文件、删掉后会失去锚点」的条目；
    empty_pages 只包含删完后一条不剩的页面。删除会保留失去锚点的正文，
    所以只有「整行只有锚点」的页面才会真的空掉。
    """
    out = {}
    for p in pages:
        if p["type"] == "source":
            continue
        title = str(p["meta"].get("title") or p["slug"])
        lines = [ln for ln in p["body"].splitlines() if ln.strip().startswith("- ")]
        # 没有锚点的正文行与本次删除无关，删哪份文件都会留下
        base_remains = sum(1 for ln in lines if not _cited_files(ln))
        per = {}
        for ln in lines:
            files = _cited_files(ln)
            if not files:
                continue
            for f in files:
                per.setdefault(f, {"statements": 0, "remains": base_remains})
                if files == {f}:
                    per[f]["statements"] += 1
                # 同一行还引用别的文件，或摘掉锚点后仍有正文，这行就不会消失
                content = ln.strip()[2:].strip()
                if files != {f} or _strip_anchor(content, f).strip():
                    per[f]["remains"] += 1
        for f, n in per.items():
            e = out.setdefault(f, {"statements": 0, "pages": [], "empty_pages": []})
            e["statements"] += n["statements"]
            e["pages"].append(title)
            if n["remains"] == 0:
                e["empty_pages"].append(title)
    return out


def _row(name, rel, entry, imp, missing, duplicate=False, paths=None):
    entry = entry or {}
    return {
        "name": name,
        "path": rel,
        "paths": paths or [],
        "duplicate": duplicate,
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
    for name, paths in raw_file_groups().items():
        if all(p.parent == config.INBOX_DIR for p in paths):  # 还在收件台、没归类的不算入库
            continue
        rel = paths[0].relative_to(config.ROOT).as_posix()
        rows.append(_row(name, rel, by_name.get(name), impact.get(name) or {},
                         missing=False, duplicate=len(paths) > 1,
                         paths=[p.relative_to(config.ROOT).as_posix() for p in paths]))
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
    if p["path"].exists():
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
    stats = {"file": name, "statements": 0, "pages_deleted": [], "removed": [],
             "needs_review": 0}
    path = find_raw_file(name)  # 同名多份会在这里抛错，绝不随便挑一份删
    entry = next((f for f in manifest["files"] if f["file"] == name), None)
    rel = path.relative_to(config.ROOT).as_posix() if path else ((entry or {}).get("path") or "")
    stem = Path(name).stem

    # 1. 主题页正文：只摘掉指向该文件的锚点，保留其余内容和无锚点条目
    pages = wiki.load_pages()
    victims = {p["path"] for p in _source_pages(pages, rel, stem)}
    for p in pages:
        if p["path"] in victims:
            continue
        sections = wiki.body_to_sections(p["body"])
        changed = 0
        unanchored = 0
        kept = {}
        for s, lns in sections.items():
            bucket = []
            for ln in lns:
                if not _cites(ln, name):
                    bucket.append(ln)
                    continue
                changed += 1
                new = _strip_anchor(ln, name)
                if not _cited_files(new):
                    unanchored += 1
                if new.strip():
                    bucket.append(new)
            kept[s] = bucket
        if not changed:
            continue
        title = str(p["meta"].get("title") or p["slug"])
        if not any(kept.values()):
            _drop_page(p, title, stats, changed, "已空")
            continue
        body = wiki.render_page(title, kept, p["type"])
        meta = dict(p["meta"])
        srcs = [s for s in (meta.get("sources") or []) if str(s) != rel]
        if not srcs:  # 来源列表被删空时，按正文剩下的锚点回填
            srcs = sorted({a[0] for a in wiki.anchors_of(body)})
        stats["statements"] += changed
        meta["sources"] = srcs
        meta["updated"] = config.today_str()
        hist = list(meta.get("history") or []) + [f"{config.today_str()} 删除 {name}（-{changed} 条）"]
        if unanchored:
            # 这一页还有无法归属到其他文件的条目：保留并标记，交给人工复核
            stats["needs_review"] += unanchored
            meta["needs_review"] = True
            meta["review_reason"] = f"删除 {name} 后有 {unanchored} 条内容失去锚点，需人工复核"
            hist.append(f"{config.today_str()} 标记需复核：{unanchored} 条内容失去锚点")
        meta["history"] = hist
        wiki.write_page(p["path"], meta, body)
        note = f"，{unanchored} 条转为需复核" if unanchored else ""
        stats["removed"].append(f"页面 {title}（-{changed} 条{note}）")

    # 2. 来源页
    for p in _source_pages(pages, rel, stem):
        if p["path"].exists():
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
    """被删掉的页面可能还被别的页面 [[双链]] 引用，把双链降级成纯文字，校验才不报错。

    只处理成功的删除：失败那一步已回滚，页面会从备份恢复，双链也必须原样保留。
    """
    gone = {t for r in results if "error" not in r for t in (r.get("pages_deleted") or [])}
    if not gone:
        return
    drop_links_to(gone)


def drop_links_to(gone):
    """把指向 gone 里标题的 [[双链]] 降级为纯文字；合并/删除后共用。"""
    for p in wiki.load_pages():
        hit = set(gone) & wiki.wikilinks_of(p["body"])
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


# ---------- 删除事务：先备份，出错整份回滚 ----------

def _backup(name):
    path = find_raw_file(name)  # 同名多份在这里就拒绝
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = config.DATA_DIR / f".purge-backup-{stamp}-{uuid.uuid4().hex[:6]}"
    bdir.mkdir(parents=True, exist_ok=True)
    for src_root in (config.WIKI_DIR, config.FIN_DIR):
        if src_root.exists():
            shutil.copytree(src_root, bdir / src_root.name, dirs_exist_ok=True)
    (bdir / "extracts").mkdir(exist_ok=True)
    for f in config.EXTRACTS_DIR.glob(Path(name).stem + ".*"):
        shutil.copy2(f, bdir / "extracts" / f.name)
    if config.MANIFEST_PATH.exists():
        shutil.copy2(config.MANIFEST_PATH, bdir / "manifest.json")
    if path:
        (bdir / "raw").mkdir(exist_ok=True)
        shutil.copy2(path, bdir / "raw" / path.name)
        (bdir / "raw_path.txt").write_text(path.relative_to(config.ROOT).as_posix(), encoding="utf-8")
    return bdir


def _restore(bdir):
    mapping = {"wiki": config.WIKI_DIR, "fin": config.FIN_DIR, "extracts": config.EXTRACTS_DIR}
    for top, dest_root in mapping.items():
        src_root = bdir / top
        if not src_root.exists():
            continue
        for src in src_root.rglob("*"):
            if not src.is_file():
                continue
            dest = dest_root / src.relative_to(src_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    if (bdir / "manifest.json").exists():
        shutil.copy2(bdir / "manifest.json", config.MANIFEST_PATH)
    raw_path = bdir / "raw_path.txt"
    if raw_path.exists():
        rel = raw_path.read_text(encoding="utf-8").strip()
        src = bdir / "raw" / Path(rel).name
        if src.exists():
            dest = config.ROOT / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def purge_files(names, task_id=None):
    results = []
    aborted = None
    try:
        for i, name in enumerate(names, 1):
            tasks.check_cancel(task_id)
            if task_id:
                tasks.update(task_id, current=i - 1, detail=f"第 {i}/{len(names)} 份：{name} 正在清理")
            bdir = None
            try:
                bdir = _backup(name)
                with MANIFEST_LOCK:
                    manifest = load_manifest()
                    r = purge_one(name, manifest)
                    save_manifest(manifest)
            except Exception as e:
                if bdir is not None:
                    try:
                        _restore(bdir)
                    except Exception as re_err:
                        e = PurgeError(f"{e}；回滚也失败：{re_err}（备份留在 "
                                       f"{bdir.relative_to(config.ROOT).as_posix()}）")
                    else:
                        e = PurgeError(f"{e}（已回滚到删除前状态，备份在 "
                                       f"{bdir.relative_to(config.ROOT).as_posix()}）")
                r = {"file": name, "error": str(e)}
                results.append(r)
                aborted = r
                break  # 一份失败就停下整批，不继续删后面的
            else:
                # 成功才清掉临时备份
                if bdir is not None:
                    shutil.rmtree(bdir, ignore_errors=True)
            results.append(r)
            if task_id:
                tasks.update(task_id, current=i)
    finally:
        _drop_dangling_links(results)
        wiki.rebuild_index_md(wiki.load_pages())
        ok = [r for r in results if "error" not in r]
        if ok:
            wiki.append_log("delete", "；".join(
                f"{r['file']}（-{r['statements']} 条，移除页面 {'、'.join(r['pages_deleted']) or '无'}"
                f"{'，' + str(r['needs_review']) + ' 条需复核' if r.get('needs_review') else ''}）"
                for r in ok))
    if aborted:
        # 让任务结果明确带上失败，而不是悄悄跳过继续
        results.append({"batch_aborted": True, "file": aborted["file"], "error": aborted["error"]})
    return results
