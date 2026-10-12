from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.db import get_session
from app.models.orm import KnowledgePage, QA
from app.schemas import QARequest, QAResponse
from app.services.qa import ask_question

log = logging.getLogger(__name__)
router = APIRouter(prefix="/qa", tags=["qa"])


@router.post("", response_model=QAResponse)
def ask(req: QARequest, session: Session = Depends(get_session)) -> QAResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        r = ask_question(req.question, session=session, asked_by=req.asked_by or "anonymous")
    except Exception as e:  # noqa: BLE001
        log.exception("问答失败")
        raise HTTPException(status_code=500, detail=f"问答失败: {e}")

    return QAResponse(
        question=r.question,
        answer_md=r.answer_md,
        selected_kpage_ids=r.selected_kpage_ids,
        out_of_scope=r.out_of_scope,
        verification=r.verification,
        citations=r.citations,
    )


@router.get("/history")
def recent_history(
    limit: int = 20,
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = (
        session.query(QA).order_by(QA.asked_at.desc()).limit(min(limit, 100)).all()
    )
    return [
        {
            "id": q.id,
            "question": q.question,
            "out_of_scope": (q.verification or {}).get("out_of_scope", False),
            "asked_by": q.asked_by,
            "asked_at": q.asked_at.isoformat() if q.asked_at else "",
            "selected_kpage_ids": q.selected_pages or [],
        }
        for q in rows
    ]
