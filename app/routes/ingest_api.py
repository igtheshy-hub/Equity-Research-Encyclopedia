from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.models.db import get_session
from app.models.orm import Report
from app.schemas import IngestStatusResponse, IngestUploadResponse
from app.services.admin import delete_report as _delete_report
from app.services.ingest import ingest_pdf_path
from app.services.library import scan_library
from app.services.pdf_extract import save_uploaded_pdf
from app.models.orm import Page

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ingest", tags=["ingest"])
_TEMPLATES = Jinja2Templates(directory="app/templates")


@router.post("/upload")
async def upload_pdf(
    files: List[UploadFile] = File(..., alias="file"),
    session: Session = Depends(get_session),
) -> dict:
    """上传一个或多个 PDF，逐个触发摄入主流程。

    - 单文件上传：返回 dict {batch, results:[{filename, report_id, status, page_count, ...}]}
    - 多文件上传：每份独立走完整 ingest_pdf_path
    - 任何一条失败不阻断其它
    """
    if not files:
        raise HTTPException(status_code=400, detail="未提供文件")

    results: list[dict] = []
    for f in files:
        name = (f.filename or "").strip() or "(未命名)"
        if not name.lower().endswith(".pdf"):
            results.append({"filename": name, "ok": False, "error": "仅接受 .pdf 文件"})
            continue
        try:
            content = await f.read()
            if not content:
                results.append({"filename": name, "ok": False, "error": "文件为空"})
                continue
            dest = save_uploaded_pdf(content, name, settings.wiki_data_dir / "reports")
            report = ingest_pdf_path(dest, session=session)
            session.refresh(report)
            results.append({
                "filename": name,
                "ok": True,
                "report_id": report.id,
                "sha256": report.sha256,
                "status": report.status,
                "page_count": report.page_count,
                "title": report.title or "",
                "message": "已编译" if report.status == "compiled" else "抽取完成，等待 LLM 编译",
            })
        except Exception as e:  # noqa: BLE001
            log.exception("摄取失败: %s", name)
            results.append({"filename": name, "ok": False, "error": repr(e)})

    return {
        "batch": len(results) > 1,
        "count": len(results),
        "ok_count": sum(1 for r in results if r.get("ok")),
        "results": results,
    }


@router.get("/library")
def library(session: Session = Depends(get_session)) -> dict:
    """扫描 data/reports/，返回每份 PDF 的摄入状态。"""
    return scan_library(session)


@router.get("/library/pane", response_class=HTMLResponse)
def library_pane(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    """HTMX partial：扫描结果直接渲染成 HTML 表格。"""
    return _TEMPLATES.TemplateResponse(
        request, "_library_pane.html", {"request": request, "lib": scan_library(session)}
    )


@router.post("/trigger")
def trigger_ingest(
    filename: str = Query(..., description="data/reports/ 下的文件名"),
    session: Session = Depends(get_session),
) -> dict:
    """从硬盘上 ingest 一份已存在的 PDF（不重新上传）。"""
    p = settings.wiki_data_dir / "reports" / filename
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"硬盘上找不到 {filename}")
    try:
        report = ingest_pdf_path(p, session=session)
        session.refresh(report)
        return {
            "ok": True,
            "filename": filename,
            "report_id": report.id,
            "status": report.status,
            "page_count": report.page_count,
            "title": report.title or "",
        }
    except Exception as e:  # noqa: BLE001
        log.exception("trigger 失败")
        return {"ok": False, "filename": filename, "error": repr(e)}


@router.post("/recompile")
def recompile(
    report_id: int = Query(..., description="已存在的 Report.id"),
    session: Session = Depends(get_session),
) -> dict:
    """重跑 LLM 编译（不重抽 PDF）；等价于 manage.py reingest --report-id N。"""
    import datetime as dt

    from app.models.orm import Page
    from app.services.ingest import _compile_with_llm
    from app.services.pdf_extract import ExtractResult, ExtractedPage

    r: Report | None = session.get(Report, report_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"未找到 r.id={report_id}")

    pages = session.query(Page).filter(Page.report_id == r.id).order_by(Page.page_no).all()
    if not pages:
        raise HTTPException(status_code=400, detail="该 Report 没有 Pages 行，无法重编译")

    extract = ExtractResult(
        report_path=None,  # type: ignore
        sha256=r.sha256,
        page_count=len(pages),
        pages=[
            ExtractedPage(
                page_no=p.page_no, text=p.text,
                ocr_used=p.ocr_used, ocr_engine=p.ocr_engine,
                char_count=p.char_count,
            )
            for p in pages
        ],
        metadata={"broker": r.broker, "title": r.title,
                  "report_date": r.report_date.isoformat() if r.report_date else "",
                  "page_count": r.page_count},
    )
    try:
        _compile_with_llm(session, r, extract)
        r.status = "compiled"
        r.error = None
        r.ingested_at = dt.datetime.now(dt.UTC)
        session.add(r)
        session.commit()
        return {"ok": True, "report_id": r.id, "status": r.status, "filename": r.original_filename}
    except Exception as e:  # noqa: BLE001
        log.exception("recompile 失败")
        r.status = "pending_llm_retry"
        r.error = repr(e)
        session.add(r)
        session.commit()
        return {"ok": False, "report_id": r.id, "error": repr(e), "filename": r.original_filename}


@router.get("/status/{report_id}", response_model=IngestStatusResponse)
def ingest_status(
    report_id: int,
    session: Session = Depends(get_session),
) -> IngestStatusResponse:
    report: Report | None = session.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report 不存在")
    return IngestStatusResponse(
        report_id=report.id,
        status=report.status,
        page_count=report.page_count,
        title=report.title,
        error=report.error,
    )


@router.delete("/{report_id}")
def delete_report(
    report_id: int,
    session: Session = Depends(get_session),
) -> dict:
    """删除一份已摄入的 Report（含 Pages / Citations 一起删）。"""
    r = session.get(Report, report_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Report 不存在")
    return _delete_report(session, report_id)