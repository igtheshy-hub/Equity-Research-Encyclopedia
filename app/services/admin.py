"""管理操作：删报告 / 删知识页 / 重命名 slug 等。

这些操作不走 LLM，全是确定性程序动作。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.orm import (
    AuditFinding,
    Citation,
    KnowledgeLink,
    KnowledgePage,
    KPageSection,
    Page,
    Report,
)

log = logging.getLogger(__name__)


def delete_report(session: Session, report_id: int, *, cascade_pages: bool = True) -> dict:
    """删除一份报告（含所有 Pages）。

    Citations 与 Report 是 FK CASCADE → 自动删。
    KnowledgePage 不会自动删（因为 KPage ↔ Report 没有 FK，靠 source_filename）。
    若该源文件名下还有其他同样的 md 文件被引用，KPage 仍可保留；
    否则一并警告 / 由调用方决定。
    """
    r = session.query(Report).filter(Report.id == report_id).one_or_none()
    if r is None:
        return {"deleted": 0, "kpage_actions": []}
    fname = r.original_filename

    n_cit = session.query(Citation).filter(Citation.report_id == report_id).delete(synchronize_session=False)
    if cascade_pages:
        n_pages = session.query(Page).filter(Page.report_id == report_id).delete(synchronize_session=False)
    else:
        n_pages = 0

    affected_kpages = (
        session.query(KnowledgePage)
        .filter(KnowledgePage.source_filename == fname)
        .all()
    )
    kpage_actions: list[dict] = []
    for kp in affected_kpages:
        other_compiled = (
            session.query(Report)
            .filter(Report.original_filename == fname)
            .filter(Report.status == "compiled")
            .filter(Report.id != report_id)
            .count()
        )
        if other_compiled == 0:
            log.info("KPage id=%d 没有其它 ingested 来源，保留并清 source_filename", kp.id)
            kp.source_filename = None
            kpage_actions.append({"kpage_id": kp.id, "action": "kept_unlinked", "title": kp.title})
        else:
            log.info("KPage id=%d 仍有其它 ingested 来源保留 source_filename", kp.id)
            kpage_actions.append({"kpage_id": kp.id, "action": "kept_linked", "title": kp.title})
        session.add(kp)

    n_audit = session.query(AuditFinding).filter(
        AuditFinding.report_ids.like(f'%"{report_id}"%')
    ).count() if False else 0
    session.delete(r)
    session.commit()

    return {
        "deleted_report_id": report_id,
        "filename": fname,
        "deleted_citations": n_cit,
        "deleted_pages": n_pages,
        "kpage_actions": kpage_actions,
    }
