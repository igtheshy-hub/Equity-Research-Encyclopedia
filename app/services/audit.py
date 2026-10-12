"""审校主流程：跨页矛盾 / 过期 / 未摄入 / 孤立。"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import defaultdict
from typing import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.llm.router import chat_with_fallback_sync
from app.models.orm import (
    AuditFinding,
    AuditRun,
    Citation,
    KPageSection,
    KnowledgeLink,
    KnowledgePage,
    Page,
    Report,
)
from app.prompts import audit_prompt, behavior_prelude
from app.services import citation as cite_svc

log = logging.getLogger(__name__)


def run_audit(session: Session) -> AuditRun:
    """完整跑一次审校；返回 AuditRun。"""
    run = AuditRun(status="running", started_at=dt.datetime.now(dt.UTC))
    session.add(run); session.commit()

    try:
        clusters = _cluster_by_topic(session)
        findings: list[AuditFinding] = []
        for topic, kpages in clusters.items():
            if len(kpages) >= 2:
                findings.extend(_compare_cluster_with_llm(session, run.id, topic, kpages))
        findings.extend(_check_isolated(session, run.id))
        findings.extend(_check_uningested(session, run.id))

        # 程序校验每条发现：quote_* 必须在 Page.text 中
        for f in findings:
            _verify_finding(f, session)

        # 落库：findings 之前只挂在 Python list；必须显式 add_all 才能写入
        if findings:
            session.add_all(findings)

        run.status = "succeeded"
        run.finished_at = dt.datetime.now(dt.UTC)
        run.summary = {
            "finding_count": len(findings),
            "by_kind": _count_by_kind(findings),
        }
        session.add(run); session.commit()
        return run

    except Exception as e:  # noqa: BLE001
        log.exception("审校失败")
        run.status = "failed"
        run.error = repr(e)
        run.finished_at = dt.datetime.now(dt.UTC)
        session.add(run); session.commit()
        raise


# ---------------------------------------------------------------------------
# 聚类
# ---------------------------------------------------------------------------
def _cluster_by_topic(session: Session) -> dict[str, list[KnowledgePage]]:
    out: dict[str, list[KnowledgePage]] = defaultdict(list)
    for kp in session.query(KnowledgePage).all():
        for tag in (kp.topic_tags or []):
            if tag:
                out[tag].append(kp)
    # 去重同一 KPage 在同一 tag 下重复
    return {tag: list({k.id: k for k in lst}.values()) for tag, lst in out.items() if len(lst) >= 1}


# ---------------------------------------------------------------------------
# 跨页比对（用 LLM）
# ---------------------------------------------------------------------------
def _compare_cluster_with_llm(
    session: Session, run_id: int, topic: str, kpages: list[KnowledgePage]
) -> list[AuditFinding]:
    kpages.sort(key=lambda k: k.updated_at or dt.datetime.min, reverse=True)

    sys_msg = behavior_prelude() + "\n\n" + audit_prompt()
    user_msg = (
        f"主题聚类：{topic}\n"
        "涉及的 KPage：\n"
        + "\n\n".join(
            f"### kpage_id={k.id} | title={k.title} | updated={k.updated_at}\n"
            + "\n".join(
                (f"## {s.heading}\n{s.body_md}" if s.heading else s.body_md)
                for s in (k.sections or [])
            )
            for k in kpages
        )
        + "\n涉及的 Report：\n"
        + "\n".join(
            f"- report_id={r.id} | original_filename={r.original_filename} | date={r.report_date}"
            for r in session.query(Report).all()
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
        log.warning("审校 LLM 失败：%s", e)
        return []

    payload = _safe_parse_json(resp.content) or {}
    out: list[AuditFinding] = []

    for c in payload.get("contradictions") or []:
        if not c.get("quote_a") or not c.get("quote_b"):
            continue
        cite_a_raw = c.get("cite_a")
        cite_b_raw = c.get("cite_b")
        ka_id = _maybe_int(cite_a_raw)
        kb_id = _maybe_int(cite_b_raw)
        f = AuditFinding(
            run_id=run_id,
            kind="contradiction",
            severity=c.get("severity") or "medium",
            summary=c.get("summary", ""),
            topic=topic,
            quote_a=c.get("quote_a"),
            cite_a=_format_cite(cite_a_raw, c.get("quote_a")),
            quote_b=c.get("quote_b"),
            cite_b=_format_cite(cite_b_raw, c.get("quote_b")),
            kpage_ids=[x for x in (ka_id, kb_id) if x],
        )
        out.append(f)

    for o in payload.get("outdated") or []:
        if not o.get("old_quote") or not o.get("new_quote"):
            continue
        cite_a_raw = o.get("cite_a")
        cite_b_raw = o.get("cite_b")
        ka_id = _maybe_int(cite_a_raw)
        kb_id = _maybe_int(cite_b_raw)
        f = AuditFinding(
            run_id=run_id,
            kind="outdated",
            severity=o.get("severity") or "medium",
            summary=o.get("summary", ""),
            topic=topic,
            quote_a=o.get("old_quote"),
            cite_a=_format_cite(cite_a_raw, o.get("old_quote")),
            quote_b=o.get("new_quote"),
            cite_b=_format_cite(cite_b_raw, o.get("new_quote")),
            kpage_ids=[x for x in (ka_id, kb_id) if x],
        )
        out.append(f)

    for u in payload.get("uningested") or []:
        f = AuditFinding(
            run_id=run_id,
            kind="uningested",
            severity="low",
            summary=u.get("summary", "") or f"主题 {topic} 邻接 PDF 未提炼",
            topic=topic,
            kpage_ids=[],
            report_ids=[int(x) for x in (u.get("related_reports") or [])],
        )
        out.append(f)

    return out


def _check_isolated(session: Session, run_id: int) -> list[AuditFinding]:
    """无访问 + 无反向链接的 KPage。"""
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.wiki_audit_isolated_days)

    def _aware(v):
        """SQLite 读出来的 datetime 是 naive，归一到 UTC-aware 以便与 cutoff 比较。"""
        if v is None:
            return None
        return v.replace(tzinfo=dt.UTC) if v.tzinfo is None else v

    inbound_pairs = (
        session.query(KnowledgeLink.dst_kpage_id)
        .distinct()
        .all()
    )
    inbound_ids = {row[0] for row in inbound_pairs}

    out: list[AuditFinding] = []
    for kp in session.query(KnowledgePage).all():
        is_orphan = (kp.access_count or 0) == 0 and kp.id not in inbound_ids
        last_acc = _aware(kp.last_accessed_at)
        too_old = last_acc is None or last_acc < cutoff
        created = _aware(kp.created_at) or dt.datetime.min.replace(tzinfo=dt.UTC)
        if is_orphan and too_old and created < cutoff:
            out.append(
                AuditFinding(
                    run_id=run_id,
                    kind="isolated",
                    severity="low",
                    summary=f"{kp.title}（slug={kp.slug}）最近 {settings.wiki_audit_isolated_days} 天无访问、无反向链接",
                    topic=(kp.topic_tags or [None])[0],
                    kpage_ids=[kp.id],
                )
            )
    return out


def _check_uningested(session: Session, run_id: int) -> list[AuditFinding]:
    """已上传但未提炼的 PDF。"""
    pdfs = (
        session.query(Report)
        .filter(or_(Report.status == "pending", Report.status == "pending_llm_retry"))
        .all()
    )
    out: list[AuditFinding] = []
    for r in pdfs:
        out.append(
            AuditFinding(
                run_id=run_id,
                kind="uningested",
                severity="medium",
                summary=f"PDF 未被提炼进任何知识页：{r.original_filename}（{r.title}）",
                topic=None,
                kpage_ids=[],
                report_ids=[r.id],
            )
        )
    return out


# ---------------------------------------------------------------------------
# 程序侧 quote 校验
# ---------------------------------------------------------------------------
def _verify_finding(f: AuditFinding, session: Session) -> None:
    """quote_a / quote_b 是否能在 cite_a / cite_b 对应 Page.text 里找到？"""
    notes: list[str] = []
    for which, quote_attr, cite_attr, report_hint_attr in [
        ("a", "quote_a", "cite_a", None),
        ("b", "quote_b", "cite_b", None),
    ]:
        quote = getattr(f, quote_attr)
        cite = getattr(f, cite_attr) or ""
        if not quote:
            continue
        # cite 可能是 "kpage:123" 或 "<file>.pdf:N" 之类，先尝试按 kpage 反查
        report_id = _resolve_finding_cite(session, f, cite, which)
        if report_id is None:
            notes.append(f"{which}: 无法定位到 Page（{cite}）")
            continue
        page_no = _extract_page_no(cite)
        if page_no is None:
            notes.append(f"{which}: cite 缺页码（{cite}）")
            continue
        page_row = (
            session.query(Page)
            .filter(Page.report_id == report_id, Page.page_no == page_no)
            .one_or_none()
        )
        if not page_row:
            notes.append(f"{which}: Page {page_no} 缺失")
            continue
        score = cite_svc.quote_in_page(page_row.text, quote)
        if score < 0.6:
            notes.append(f"{which}: 原文比对未命中 (score={score:.2f})")
    f.verification_note = "\n".join(notes) if notes else None


def _resolve_finding_cite(session: Session, f: AuditFinding, cite: str, which: str) -> int | None:
    """从 cite_a/cite_b 字符串里反查 report_id。

    支持两种格式：
      - '<original_filename>.pdf:<page>'  ← 推荐：直接给出文件名与页码
      - 'kpage:<id>'                     ← 旧/兼容：通过 KPage → Section → Citation 间接反查
    """
    cite = (cite or "").strip()
    if not cite:
        return None
    if cite.startswith("kpage:"):
        kid = _maybe_int(cite.split(":", 1)[1])
        if kid:
            # 选该 KPage 在 f.kpage_ids 之外的另一端
            other = [k for k in (f.kpage_ids or []) if str(k) != str(kid)]
            other_kid = other[0] if other else kid
            cit = (
                session.query(Citation)
                .filter(Citation.section_id.in_(
                    session.query(KPageSection.id).filter(KPageSection.kpage_id == other_kid)
                ))
                .first()
            )
            return cit.report_id if cit else None
        return None
    # filename.pdf[:N] 或 filename.pdf#pN 形式：以文件名（切首段分隔符）查 Report
    fn_part = re.split(r"[:#]", cite, maxsplit=1)[0].strip()
    if not fn_part:
        return None
    r = (
        session.query(Report)
        .filter(Report.original_filename == fn_part)
        .one_or_none()
    )
    return r.id if r else None


def _extract_page_no(cite: str) -> int | None:
    """同时认两种格式：`<fn>:<N>` 与 `<fn>#p<N>`（Igshy 新格式）。"""
    if not cite:
        return None
    # 优先认 :N，再认 #pN
    m = re.search(r"#p(\d+)", cite)
    if m:
        return int(m.group(1))
    m = re.search(r":(\d+)", cite)
    if m:
        return int(m.group(1))
    return None


def _format_cite(raw, quote: str) -> str:
    """LLM 给出的 cite：可能是 int kpage_id 或 str token (推荐 'file.pdf:N')。

    透传字符串、原样保留为 'kpage:<id>' 形式或兜底 'unknown:<quote[:30]>'。
    """
    if isinstance(raw, int):
        return f"kpage:{raw}"
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"unknown:{(quote or '')[:30]}"


def _maybe_int(x) -> int | None:
    try:
        return int(x)
    except Exception:  # noqa: BLE001
        return None


def _count_by_kind(items: Iterable[AuditFinding]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for it in items:
        out[it.kind] += 1
    return dict(out)


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
