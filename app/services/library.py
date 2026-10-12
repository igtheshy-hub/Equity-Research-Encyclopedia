"""data/reports/ 目录扫描 + 摄入状态对照。

把"硬盘里有啥"和"库里已摄入啥"对照成一个视图：
- status=not_ingested    硬盘有，库里没有
- status=duplicate       硬盘有，文件名不在库里但 sha256 已被摄入（同名不同 hash 或同 hash 不同名）
- status=compiled        库里已编译
- status=pending_llm_retry
- status=failed
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models.orm import Report

log = logging.getLogger(__name__)


def _sha256_file(p: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def scan_library(session: Session) -> dict:
    """扫描 reports/ 目录并与 DB 状态对照。"""
    reports_dir: Path = settings.wiki_data_dir / "reports"
    if not reports_dir.exists():
        return {
            "reports_dir": str(reports_dir),
            "exists": False,
            "total": 0,
            "by_status": {},
            "files": [],
        }

    by_filename: dict[str, Report] = {
        r.original_filename: r for r in session.query(Report).all()
    }
    by_sha: dict[str, Report] = {r.sha256: r for r in session.query(Report).all()}

    files: list[dict] = []
    for pdf_path in sorted(reports_dir.iterdir()):
        if not pdf_path.is_file():
            continue
        if pdf_path.suffix.lower() != ".pdf":
            continue
        fname = pdf_path.name
        st = pdf_path.stat()
        size_kb = round(st.st_size / 1024, 1)
        mtime = dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="minutes")

        report = by_filename.get(fname)
        note: str | None = None
        if report is not None:
            status = report.status
            report_id = report.id
            title = report.title or ""
            broker = report.broker or ""
        else:
            # 文件名不在库里，按 sha256 反查：可能是同一份内容用了不同文件名
            try:
                sha = _sha256_file(pdf_path)
            except OSError as e:  # noqa: BLE001
                files.append({
                    "filename": fname,
                    "size_kb": size_kb,
                    "mtime": mtime,
                    "status": "unreadable",
                    "report_id": None,
                    "title": "",
                    "broker": "",
                    "note": f"无法读取: {e}",
                })
                continue
            same = by_sha.get(sha)
            if same is not None:
                status = "duplicate"
                report_id = same.id
                title = same.title or ""
                broker = same.broker or ""
                note = f"内容与已摄入的「{same.original_filename}」相同"
            else:
                status = "not_ingested"
                report_id = None
                title = ""
                broker = ""
                note = None

        files.append({
            "filename": fname,
            "size_kb": size_kb,
            "mtime": mtime,
            "status": status,
            "report_id": report_id,
            "title": title,
            "broker": broker,
            "note": note,
        })

    counts: dict[str, int] = {}
    for f in files:
        counts[f["status"]] = counts.get(f["status"], 0) + 1

    return {
        "reports_dir": str(reports_dir),
        "exists": True,
        "total": len(files),
        "by_status": counts,
        "files": files,
    }