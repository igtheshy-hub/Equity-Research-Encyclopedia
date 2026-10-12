"""管理操作：删报告 / 重跑摄入 等。

不动 LLM，全是确定性程序动作。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from . import config, wiki
from .classify import sha256_of

log = logging.getLogger(__name__)


def _find_manifest_entry(filename: str | None = None, sha256: str | None = None) -> Optional[dict]:
    """在 manifest.json 里按文件名或 sha 找一条记录。"""
    if not config.MANIFEST_PATH.exists():
        return None
    m = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    for entry in m.get("files") or []:
        if filename and entry.get("file") == filename:
            return entry
        if sha256 and entry.get("sha256") == sha256:
            return entry
    return None


def _strip_source_from_pages(rel_path: str) -> int:
    """把 `sources:` 列表里包含 rel_path 的主题页去掉该项，返回清理过的页面数。

    只改 frontmatter，不动正文（正文里的 `[[file.pdf#pN]]` 锚点由 wiki 单独处理）。
    若一个页面 sources 删空，删整页（避免空指针）。
    """
    touched = 0
    for p in wiki.load_pages():
        if p["type"] == "source":
            continue
        meta = p["meta"]
        sources = list(meta.get("sources") or [])
        if rel_path not in sources:
            continue
        sources = [s for s in sources if s != rel_path]
        if not sources:
            # 全部来源被清空 → 删整页
            try:
                p["path"].unlink()
                log.info("页面 %s 的 sources 全部清空，已删除整页", p["path"].name)
            except OSError as e:
                log.warning("删除 %s 失败: %s", p["path"], e)
        else:
            meta["sources"] = sources
            # 不重写正文，只更新 meta 与 history
            hist = list(meta.get("history") or [])
            hist.append(f"{config.today_str()} 删除来源 {Path(rel_path).name}")
            meta["history"] = hist
            meta["updated"] = config.today_str()
            # 复用 wiki.write_page 保持 frontmatter 风格一致
            from .wiki import write_page
            body = p["body"]  # 正文不动
            write_page(p["path"], meta, body)
        touched += 1
    return touched


def _strip_anchors_from_body(body: str, fname: str) -> tuple[str, int]:
    """把正文中 `[[fname#pN]]` / `[[fname#paraN]]` 这种锚点删掉（不删带 `[[其它文件名...]]`）。

    返回 (cleaned_body, removed_count)。
    """
    pat = re.compile(r"\[\[" + re.escape(fname) + r"#[^\]]+\]\]")
    cleaned, n = pat.subn("", body)
    return cleaned, n


def delete_ingested(
    filename: str | None = None,
    sha256: str | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    """删除一份已摄入报告的全部痕迹。

    输入：filename（如 "中信证券_xxx.pdf"）或 sha256。
    删除内容：
      - data/manifest.json 的对应条目
      - wiki/sources/{stem}.md
      - wiki/{companies,industries,concepts}/*.md 的 sources: 数组里这一项
      - 那些页面正文里的 `[[file#N]]` 锚点（清理，不删整段；其余文件不受影响）
    若主题页 sources 删空 → 整页删除（避免空指针页）。

    返回 dict { manifest_removed, source_page_deleted, topic_pages_touched, anchors_removed }
    """
    entry = _find_manifest_entry(filename=filename, sha256=sha256)
    if entry is None:
        ident = filename or sha256 or "(未指定)"
        return {"found": False, "message": f"manifest 中未找到 {ident}"}

    fname = entry["file"]
    rel = entry.get("path") or ""  # 形如 raw/reports/汽车/xxx.pdf
    stem = Path(fname).stem

    result = {
        "found": True,
        "filename": fname,
        "manifest_removed": False,
        "source_page_deleted": False,
        "topic_pages_touched": 0,
        "anchors_removed": 0,
    }

    if dry_run:
        return {**result, "dry_run": True}

    # 1. manifest 删条
    m = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    m["files"] = [f for f in m.get("files") or [] if f.get("sha256") != entry["sha256"]]
    config.MANIFEST_PATH.write_text(
        json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    result["manifest_removed"] = True
    log.info("manifest 条目已删除：%s", fname)

    # 2. 删 wiki/sources/{stem}.md（来源摘要页）
    src_page = config.TYPE_DIRS["source"] / f"{stem}.md"
    if src_page.exists():
        try:
            src_page.unlink()
            result["source_page_deleted"] = True
            log.info("来源页已删除：%s", src_page.name)
        except OSError as e:
            log.warning("删除 %s 失败: %s", src_page, e)

    # 3. 主题页 sources: 数组移除 + 正文锚点清理
    if rel:
        result["topic_pages_touched"] = _strip_source_from_pages(rel)
        for p in wiki.load_pages():
            if p["type"] == "source" or not p["path"].exists():
                continue
            meta = p["meta"]
            if rel not in (meta.get("sources") or []):
                continue
            # 清理正文里该文件的锚点
            cleaned, n = _strip_anchors_from_body(p["body"], fname)
            if n > 0:
                from .wiki import write_page
                write_page(p["path"], meta, cleaned)
                result["anchors_removed"] += n
                log.info("页面 %s 清理了 %d 个锚点", p["path"].name, n)

    # 4. 重建 wiki/index.md（条目变化了）
    wiki.rebuild_index_md(wiki.load_pages())
    wiki.append_log("delete", f"删除 {fname}")

    return result


def list_ingested() -> list[dict]:
    """列出所有已摄入文件。"""
    if not config.MANIFEST_PATH.exists():
        return []
    m = json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    return m.get("files") or []


def list_uningested() -> list[dict]:
    """扫 raw/ 下所有支持格式的文件，列出未在 manifest 中的（按文件名匹配）。"""
    from .pdf_extract import SUPPORTED_SUFFIXES
    ingested_files = {f.get("file") for f in list_ingested()}
    out = []
    for d in (config.REPORTS_DIR, config.FILINGS_DIR, config.POLICIES_DIR):
        for suf in SUPPORTED_SUFFIXES:
            for p in sorted(d.rglob("*" + suf)):
                if p.name in ingested_files:
                    continue
                rel = p.relative_to(config.ROOT).as_posix()
                out.append({
                    "file": p.name,
                    "path": rel,
                    "size_kb": round(p.stat().st_size / 1024, 1),
                    "kind": "report" if "reports" in rel else
                            "filing" if "filings" in rel else
                            "policy" if "policies" in rel else "unknown",
                })
    return out