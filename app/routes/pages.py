from __future__ import annotations

import logging
from collections import Counter, defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.db import get_session
from app.models.orm import (
    AuditFinding,
    AuditRun,
    Citation,
    KnowledgeLink,
    KnowledgePage,
    Page as PageORM,
    QA,
    Report,
)
from app.services import md_render as md_renderer

log = logging.getLogger(__name__)
router = APIRouter(tags=["pages"])

templates = Jinja2Templates(directory="app/templates")
# 把 markdown 渲染与 citations 提取注册成 Jinja 过滤器
templates.env.filters["md"] = md_renderer.render  # type: ignore[attr-defined]
templates.env.filters["cites_in"] = md_renderer.citations_in  # type: ignore[attr-defined]


def _ctx(request: Request, **extra) -> dict:
    base = {"request": request, "site_name": "研报团队百科"}
    base.update(extra)
    return base


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    kpages = (
        session.query(KnowledgePage)
        .order_by(KnowledgePage.updated_at.desc())
        .limit(20)
        .all()
    )
    recent_qa = (
        session.query(QA).order_by(QA.asked_at.desc()).limit(10).all()
    )
    finding_count = session.query(AuditFinding).count()
    report_count = session.query(KnowledgePage).count()
    tags_counter: Counter[str] = Counter()
    for kp in session.query(KnowledgePage).all():
        for t in (kp.topic_tags or []):
            tags_counter[t] += 1
    tags = tags_counter.most_common(30)
    return templates.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            kpages=kpages, qa=recent_qa,
            finding_count=finding_count,
            kpage_count=report_count,
            tags=tags,
        ),
    )


@router.get("/ingest", response_class=HTMLResponse)
def ingest_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    from app.models.orm import Report

    reports = (
        session.query(Report).order_by(Report.created_at.desc()).limit(50).all()
    )
    return templates.TemplateResponse(
        request,
        "ingest.html",
        _ctx(
            request,
            reports=reports,
            reports_dir=str(settings.wiki_data_dir / "reports"),
        ),
    )


@router.get("/qa", response_class=HTMLResponse)
def qa_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    history = (
        session.query(QA).order_by(QA.asked_at.desc()).limit(20).all()
    )
    return templates.TemplateResponse(request, "qa.html", _ctx(request, history=history))


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    latest_run = session.query(AuditRun).order_by(AuditRun.id.desc()).first()
    findings = []
    if latest_run:
        findings = (
            session.query(AuditFinding)
            .filter(AuditFinding.run_id == latest_run.id)
            .order_by(AuditFinding.kind, AuditFinding.severity)
            .all()
        )
    runs = session.query(AuditRun).order_by(AuditRun.id.desc()).limit(5).all()
    return templates.TemplateResponse(
        request,
        "audit.html",
        _ctx(request, latest_run=latest_run, findings=findings, runs=runs),
    )


@router.get("/k/{slug}", response_class=HTMLResponse)
def kpage_view(
    slug: str,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    kpage = (
        session.query(KnowledgePage).filter(KnowledgePage.slug == slug).one_or_none()
    )
    if not kpage:
        raise HTTPException(status_code=404, detail="知识页不存在")

    related = []
    if kpage.topic_tags:
        # 反向链接：与其共享 tag 的其它 KPage（Python 端过滤 JSON 列表）
        cand = (
            session.query(KnowledgePage)
            .filter(KnowledgePage.id != kpage.id)
            .all()
        )
        related = [k for k in cand if any(t in (k.topic_tags or []) for t in kpage.topic_tags)]
        related = related[:30]
    return templates.TemplateResponse(
        request,
        "kpage.html",
        _ctx(request, kpage=kpage, related=related),
    )


@router.get("/k/{slug}/export.md")
def kpage_export_markdown(
    slug: str,
    session: Session = Depends(get_session),
) -> "Response":
    """导出知识页为 Markdown：每节正文 + 引用清单（可粘到 Notion / Obsidian）。"""
    from fastapi.responses import PlainTextResponse

    kpage = (
        session.query(KnowledgePage).filter(KnowledgePage.slug == slug).one_or_none()
    )
    if not kpage:
        raise HTTPException(status_code=404, detail="知识页不存在")

    parts: list[str] = []
    parts.append(f"# {kpage.title}\n")
    if kpage.brief:
        parts.append(f"> {kpage.brief}\n")
    if kpage.topic_tags:
        parts.append("**标签**: " + " · ".join(kpage.topic_tags) + "\n")
    parts.append(f"\n*slugs: `{kpage.slug}` · v{kpage.version}*\n")

    cites: list[str] = []
    for sec in (kpage.sections or []):
        parts.append(f"\n## {sec.heading or '未命名小节'}\n")
        parts.append(sec.body_md or "")
        parts.append("")
        # 收集引用 token
        from app.services.citation import extract_citations
        for ref in extract_citations(sec.body_md or ""):
            cites.append(f"- {ref.filename}: 第 {ref.page_no} 页 — `{ref.preceding_text}`")

    if cites:
        parts.append("\n## 引用清单\n")
        parts.extend(cites)

    return PlainTextResponse(
        "\n".join(parts),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}.md"'},
    )


@router.get("/api/pages/quote")
def api_pages_quote(
    file: str,
    page: int,
    session: Session = Depends(get_session),
) -> dict:
    """给 citation.js 悬浮引用用的接口。

    返回该页的简短摘录；超长时截断 200 字。
    """
    report: Report | None = (
        session.query(Report).filter(Report.original_filename == file).one_or_none()
    )
    if not report:
        raise HTTPException(status_code=404, detail=f"未找到文件名: {file}")
    p: PageORM | None = (
        session.query(PageORM)
        .filter(PageORM.report_id == report.id, PageORM.page_no == page)
        .one_or_none()
    )
    if not p:
        raise HTTPException(status_code=404, detail=f"未找到 {file} 第 {page} 页")
    snippet = (p.text or "")[:240]
    if len(p.text or "") > 240:
        snippet += "…"
    return {
        "file": file,
        "page_no": page,
        "report_id": report.id,
        "quote": snippet,
        "ocr_used": p.ocr_used,
    }


@router.get("/viewer", response_class=HTMLResponse)
def viewer(
    request: Request,
    file: str = Query(..., description="PDF 原文件名"),
    page: int = Query(..., ge=1),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """原文对照视图：单页文本 + 引用高亮 + 页码导航。

    - ?file=X&page=N&highlight=<quote>：高亮首个匹配 quote 的片段
    - 不依赖 PDF 阅读器，纯文本即可核对（PDF 文件本身可走 `/api/reports/{file}/pdf` 下载）
    """
    report: Report | None = (
        session.query(Report).filter(Report.original_filename == file).one_or_none()
    )
    if not report:
        raise HTTPException(status_code=404, detail=f"未找到文件名: {file}")
    pg: PageORM | None = (
        session.query(PageORM)
        .filter(PageORM.report_id == report.id, PageORM.page_no == page)
        .one_or_none()
    )
    if not pg:
        raise HTTPException(status_code=404, detail=f"未找到 {file} 第 {page} 页")

    all_pages: list[PageORM] = (
        session.query(PageORM)
        .filter(PageORM.report_id == report.id)
        .order_by(PageORM.page_no)
        .all()
    )

    return templates.TemplateResponse(
        request,
        "viewer.html",
        _ctx(
            request,
            report=report,
            page=pg,
            all_pages=all_pages,
            file=file,
        ),
    )


@router.get("/api/reports/{filename}/pdf")
def api_report_pdf(
    filename: str,
    session: Session = Depends(get_session),
):
    """直接返回 PDF 二进制（供 PDF.js / embed / 外链打开 PDF 源文件用）。"""
    from fastapi.responses import FileResponse

    report: Report | None = (
        session.query(Report).filter(Report.original_filename == filename).one_or_none()
    )
    if not report:
        raise HTTPException(status_code=404, detail=f"未找到文件名: {filename}")
    p = Path(report.file_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"硬盘上找不到该 PDF")
    return FileResponse(
        str(p),
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
