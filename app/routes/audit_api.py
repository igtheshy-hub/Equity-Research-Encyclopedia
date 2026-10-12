from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.models.db import get_session
from app.models.orm import AuditFinding, AuditRun
from app.schemas import AuditRunResponse
from app.services.audit import run_audit

log = logging.getLogger(__name__)
router = APIRouter(prefix="/audit", tags=["audit"])


@router.post("/run", response_model=AuditRunResponse)
def audit_run(session: Session = Depends(get_session)) -> AuditRunResponse:
    try:
        run = run_audit(session)
    except Exception as e:  # noqa: BLE001
        log.exception("审校失败")
        raise HTTPException(status_code=500, detail=f"审校失败: {e}")
    return AuditRunResponse(
        run_id=run.id,
        status=run.status,
        started_at=run.started_at.isoformat() if run.started_at else "",
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        summary=run.summary or {},
        error=run.error,
    )


@router.get("/findings")
def list_findings(
    run_id: Optional[int] = None,
    kind: Optional[str] = None,
    limit: int = Query(default=100, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    q = session.query(AuditFinding)
    if run_id is not None:
        q = q.filter(AuditFinding.run_id == run_id)
    if kind:
        q = q.filter(AuditFinding.kind == kind)
    rows = q.order_by(AuditFinding.id.desc()).limit(limit).all()
    return [
        {
            "id": f.id,
            "run_id": f.run_id,
            "kind": f.kind,
            "severity": f.severity,
            "topic": f.topic,
            "summary": f.summary,
            "quote_a": f.quote_a,
            "cite_a": f.cite_a,
            "quote_b": f.quote_b,
            "cite_b": f.cite_b,
            "kpage_ids": f.kpage_ids or [],
            "report_ids": f.report_ids or [],
            "verification_note": f.verification_note,
            "created_at": f.created_at.isoformat() if f.created_at else "",
        }
        for f in rows
    ]


@router.get("/runs")
def list_runs(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.query(AuditRun).order_by(AuditRun.id.desc()).limit(20).all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else "",
            "finished_at": r.finished_at.isoformat() if r.finished_at else "",
            "summary": r.summary or {},
        }
        for r in rows
    ]
