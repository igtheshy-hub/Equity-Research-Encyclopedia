"""两阶段问答：选页 → 综合回答。"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.llm.router import chat_with_fallback_sync
from app.models.orm import (
    Citation,
    KPageSection,
    KnowledgePage,
    QA,
)
from app.prompts import (
    behavior_prelude,
    qa_answer_prompt,
    qa_select_prompt,
)
from app.services import citation as cite_svc
from app.services.retrieval import search

log = logging.getLogger(__name__)


@dataclass
class QAResult:
    question: str
    answer_md: str
    selected_kpage_ids: list[int]
    citations: list[dict]
    verification: dict
    out_of_scope: bool
    raw_select: dict | None = None


def ask_question(
    question: str,
    *,
    session: Session,
    asked_by: str = "anonymous",
    top_k: int | None = None,
) -> QAResult:
    """完整两阶段问答；返回结构化结果，可落 QA 历史。"""
    question = (question or "").strip()
    if not question:
        return QAResult(
            question="", answer_md="", selected_kpage_ids=[],
            citations=[], verification={"error": "empty_question"},
            out_of_scope=True,
        )

    top_k = top_k or 8
    candidates = _candidate_block(session, question, limit=30)
    if not candidates:
        qa_row = QA(
            question=question,
            selected_pages=[],
            answer_md=_out_of_scope_md(question),
            citations=[],
            verification={"out_of_scope": True, "reason": "no_candidates"},
            asked_by=asked_by,
            asked_at=dt.datetime.now(dt.UTC),
        )
        session.add(qa_row)
        session.commit()
        return QAResult(
            question=question, answer_md=qa_row.answer_md,
            selected_kpage_ids=[], citations=[], verification=qa_row.verification,
            out_of_scope=True,
        )

    # ---- 阶段 1：选页 ----
    selected, raw_select = _select_pages(question, candidates)
    if not selected:
        answer_md = _out_of_scope_md(question, hint=_hint_from_candidates(candidates))
        qa_row = QA(
            question=question, selected_pages=[],
            answer_md=answer_md, citations=[],
            verification={"out_of_scope": True, "select": raw_select},
            asked_by=asked_by, asked_at=dt.datetime.now(dt.UTC),
        )
        session.add(qa_row); session.commit()
        return QAResult(question=question, answer_md=answer_md,
                        selected_kpage_ids=[], citations=[],
                        verification=qa_row.verification, out_of_scope=True,
                        raw_select=raw_select)

    # ---- 阶段 2：作答应 ----
    full_pages = _load_kpages_with_sections(session, selected)
    answer_md = _answer_with_llm(question, full_pages)

    # 去掉模型输出里残留的 <think>...</think> 思考块（MiniMax / DeepSeek 等
    # 会在答案前留推理痕迹）。否则引文回查会把思考块里的文字当成"事实句
    # 尾部"去比对，自然命中不上；展示给用户的答案也不该夹带。
    _THINK_RE = re.compile(r"<think>.*?</think>\s*", flags=re.DOTALL)
    answer_md = _THINK_RE.sub("", answer_md).strip()
    body_for_verify = answer_md

    # ---- 验证 & 落历史 ----
    resolver = _build_resolver(session)
    verified = cite_svc.verify_body_md(body_for_verify, resolver=resolver)
    citations_payload = [
        {
            "filename": v.ref.filename,
            "page_no": v.ref.page_no,
            "report_id": v.report_id,
            "score": v.match_score,
            "verified": v.verified,
            "needs_review": v.needs_review,
            "quote": v.ref.preceding_text,
        }
        for v in verified
    ]
    verification = {
        "tokens_total": len(verified),
        "tokens_verified": sum(1 for v in verified if v.verified),
        "needs_review": [v.ref.filename + ":" + str(v.ref.page_no)
                         for v in verified if v.needs_review],
        "unresolved": [v.ref.filename + ":" + str(v.ref.page_no)
                       for v in verified if v.report_id is None],
    }
    qa_row = QA(
        question=question,
        selected_pages=selected,
        answer_md=answer_md,
        citations=citations_payload,
        verification=verification,
        asked_by=asked_by,
        asked_at=dt.datetime.now(dt.UTC),
    )
    session.add(qa_row); session.commit()

    # 更新被引用 KPage 的最近访问时间 / 计数
    session.query(KnowledgePage).filter(KnowledgePage.id.in_(selected)).update(
        {
            KnowledgePage.access_count: KnowledgePage.access_count + 1,
            KnowledgePage.last_accessed_at: dt.datetime.now(dt.UTC),
        },
        synchronize_session=False,
    )
    session.commit()

    return QAResult(
        question=question, answer_md=answer_md,
        selected_kpage_ids=selected, citations=citations_payload,
        verification=verification, out_of_scope=False, raw_select=raw_select,
    )


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------
def _candidate_block(session: Session, query: str, limit: int) -> list[dict]:
    """FTS5 → 候选列表（含 topic_tags / updated_at）。"""
    rows = search(session, query, limit=limit)
    if rows:
        ids = [r[0] for r in rows]
        kps = (
            session.query(KnowledgePage).filter(KnowledgePage.id.in_(ids)).all()
        )
        by_id = {k.id: k for k in kps}
        out = []
        for kid, score, meta in rows:
            kp = by_id.get(kid)
            if not kp:
                continue
            out.append(
                {
                    "kpage_id": kp.id,
                    "title": kp.title,
                    "brief": kp.brief,
                    "topic_tags": kp.topic_tags or [],
                    "updated_at": kp.updated_at.isoformat() if kp.updated_at else "",
                    "access_count": kp.access_count or 0,
                    "score": score,
                }
            )
        return out
    # 回退：直接按 updated_at 选最新 N 条
    kps = (
        session.query(KnowledgePage)
        .order_by(KnowledgePage.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "kpage_id": kp.id,
            "title": kp.title,
            "brief": kp.brief,
            "topic_tags": kp.topic_tags or [],
            "updated_at": kp.updated_at.isoformat() if kp.updated_at else "",
            "access_count": kp.access_count or 0,
            "score": 0.0,
        }
        for kp in kps
    ]


def _select_pages(question: str, candidates: list[dict]) -> tuple[list[int], dict | None]:
    sys_msg = behavior_prelude() + "\n\n" + _select_prompt_text()
    user_msg = (
        f"问题：{question}\n\n"
        "候选 KPage：\n" + "\n".join(
            f"- kpage_id={c['kpage_id']} | title={c['title']} | "
            f"tags={'/'.join(c['topic_tags'])} | brief={c['brief']}"
            for c in candidates
        )
    )
    try:
        resp = chat_with_fallback_sync(
            [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("选页 LLM 失败：%s", e)
        # 退化：直接按 bm25 score 取前 5
        picked = [c["kpage_id"] for c in candidates[:5]]
        return picked, {"selected": picked, "fallback": "no_llm"}

    payload = _safe_parse_json(resp.content)
    if payload is None:
        picked = [c["kpage_id"] for c in candidates[:5]]
        return picked, {"selected": picked, "fallback": "parse_failed"}

    selected = [int(x) for x in (payload.get("selected") or []) if isinstance(x, int)]
    # 兜底过滤：只保留真实存在于 candidates 的 id
    cand_ids = {c["kpage_id"] for c in candidates}
    selected = [s for s in selected if s in cand_ids]
    return selected, payload


def _select_prompt_text() -> str:
    return qa_select_prompt()


def _load_kpages_with_sections(session: Session, kpage_ids: list[int]) -> list[KnowledgePage]:
    return (
        session.query(KnowledgePage)
        .filter(KnowledgePage.id.in_(kpage_ids))
        .all()
    )


def _answer_with_llm(question: str, kpages: list[KnowledgePage]) -> str:
    if not kpages:
        return _out_of_scope_md(question)

    sys_msg = behavior_prelude() + "\n\n" + qa_answer_prompt()
    user_msg = (
        f"用户问题：{question}\n\n候选 KPage 全文：\n"
        + "\n\n".join(
            f"=== kpage_id={k.id} | slug={k.slug} | title={k.title} | tags={k.topic_tags} ===\n"
            + "\n".join(
                (f"## {s.heading}\n{s.body_md}" if s.heading else s.body_md)
                for s in (k.sections or [])
            )
            for k in kpages
        )
    )
    try:
        resp = chat_with_fallback_sync(
            [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
        )
        return resp.content.strip() or _out_of_scope_md(question)
    except Exception as e:  # noqa: BLE001
        log.warning("作答 LLM 失败：%s", e)
        return _out_of_scope_md(question, hint="（LLM 调用失败，建议稍后重试）")


def _build_resolver(session: Session) -> cite_svc.FilenameResolver:
    from app.models.orm import Page, Report

    rows = session.query(Report).all()
    report_index = []
    for r in rows:
        pages = session.query(Page).filter(Page.report_id == r.id).all()
        pages_dict = {p.page_no: p.text for p in pages}
        report_index.append((r.id, r.original_filename, pages_dict))
    return cite_svc.FilenameResolver.build(report_index)


def _out_of_scope_md(question: str, hint: str = "") -> str:
    base = (
        f"知识库中暂无关于「{question}」的直接记录，建议"
    )
    if hint:
        return base + hint
    return (
        f"知识库中暂无关于「{question}」的直接记录，"
        "请咨询研究助理、查阅原 PDF，或在 `/ingest` 页面先将相关研报拖入系统后再来提问。"
    )


def _hint_from_candidates(candidates: list[dict]) -> str:
    if not candidates:
        return ""
    titles = "、".join(c["title"] for c in candidates[:3])
    return f"可参考知识库中相近主题的页面：{titles}"


def _safe_parse_json(text: str) -> dict | None:
    if not text:
        return None
    s = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        s = m.group(1)
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        i = s.find("{")
        j = s.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except Exception:  # noqa: BLE001
                return None
        return None
