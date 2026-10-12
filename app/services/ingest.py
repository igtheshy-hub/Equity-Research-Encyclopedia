"""摄入主流程：从一份 PDF → 知识页 + 引用。

调用顺序：
1. extract_pdf()       → 报告 + 逐页文本
2. get_or_create_report()
3. _llm_compile()      → JSON（action / sections / topic_tags / reason）
4. extract_citations() → 每节引用
5. verify_body_md()    → 引用回查
6. persist()           → 写入 KnowledgePage / KPageSection / Citation

LLM 调用失败或产出无法解析时，把 Report 标记为 pending_llm_retry，下一次手动重试。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.llm.router import chat_with_fallback_sync
from app.llm.base import LLMMessage, LLMOptions
from app.models.orm import (
    Citation,
    KPageSection,
    KnowledgePage,
    Page,
    Report,
)
from app.prompts import (
    behavior_prelude,
    ingest_prompt as _ingest_prompt,
)
from app.services import citation as cite_svc
from app.services import wiki_export
from app.services.pdf_extract import ExtractResult, extract_pdf
from app.services.retrieval import rebuild_index

log = logging.getLogger(__name__)


def ingest_pdf_path(pdf_path: Path, *, session: Session) -> Report:
    """端到端摄取：抽取 → LLM 编译 → 验证 → 落库。"""
    pdf_path = Path(pdf_path)
    extract = extract_pdf(
        pdf_path,
        text_layer_threshold=settings.wiki_ocr_text_layer_threshold,
        page_text_max_chars=settings.wiki_page_text_max_chars,
        enable_ocr=settings.wiki_enable_ocr,
        ocr_engine=settings.wiki_ocr_engine,
        ocr_lang=settings.wiki_ocr_lang,
    )

    report = _persist_report(session, extract)
    try:
        _compile_with_llm(session, report, extract)
        report.status = "compiled"
        report.ingested_at = dt.datetime.now(dt.UTC)
        session.add(report)
        session.commit()
        rebuild_index(session)
    except Exception as e:  # noqa: BLE001
        log.exception("LLM 编译失败")
        report.status = "pending_llm_retry"
        report.error = repr(e)
        session.add(report)
        session.commit()
    return report


def _sha256_file(p: Path) -> str:
    """小工具：算硬盘上文件的 sha256（用于预创建 Report 行的去重）。"""
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def start_ingest_async(
    session: Session,
    pdf_path: Path,
    *,
    background_tasks,
    original_filename: str | None = None,
) -> Report:
    """预创建 Report 行（status='extracting'）+ 把全流程挂到 BackgroundTasks。

    调用方立即拿到 Report 行（status='extracting'，page_count=0）；
    后台线程跑 extract + LLM compile，最终改 status 到 'compiled' / 'pending_llm_retry'。

    文件已在硬盘时使用（上传后已 `save_uploaded_pdf` 落盘）。同名文件 sha 相同 → 复用旧行。
    """
    pdf_path = Path(pdf_path)
    sha = _sha256_file(pdf_path)
    fname = original_filename or pdf_path.name
    report = session.query(Report).filter(Report.sha256 == sha).one_or_none()
    if report is None:
        report = Report(
            sha256=sha,
            file_path=str(pdf_path),
            original_filename=fname,
            status="extracting",
            page_count=0,
        )
        session.add(report)
        session.flush()
    else:
        # 重新走一次：清状态、清旧 pages 留给 _persist_report 删
        report.status = "extracting"
        report.error = None
        report.page_count = 0
        session.add(report)
        session.flush()
    session.commit()
    session.refresh(report)
    # 排队后台任务（在新线程跑，独立 DB session）
    background_tasks.add_task(_run_full_ingest_bg, report.id, str(pdf_path))
    return report


def _run_full_ingest_bg(report_id: int, pdf_path_str: str) -> None:
    """BackgroundTasks 入口函数。在独立 session 里跑完整 ingest_pdf_path。

    设计原则：
    - 出错只把 status 改 pending_llm_retry，绝不抛异常（BackgroundTasks 不易观察）
    - 不依赖任何 in-flight session（请求已结束）
    - 不再 commit 任何 session（ingest_pdf_path 自己 commit）
    """
    from app.models.db import session_scope
    pdf_path = Path(pdf_path_str)
    with session_scope() as s:
        r = s.get(Report, report_id)
        if r is None:
            log.error("[ingest-bg] report id=%d 不存在，跳过", report_id)
            return
        log.info("[ingest-bg] 开始 report id=%d file=%s", report_id, r.original_filename)
        try:
            ingest_pdf_path(pdf_path, session=s)
            log.info("[ingest-bg] 完成 report id=%d status=%s",
                     report_id, s.get(Report, report_id).status)
        except Exception as e:  # noqa: BLE001
            log.exception("[ingest-bg] 异常 report id=%d", report_id)
            r2 = s.get(Report, report_id)
            if r2 is not None:
                r2.status = "pending_llm_retry"
                r2.error = repr(e)
                s.add(r2)
                s.commit()


def _persist_report(session: Session, extract: ExtractResult) -> Report:
    # 已存在（按 sha256）→ 刷新 Pages + 重新走 compile
    report = (
        session.query(Report).filter(Report.sha256 == extract.sha256).one_or_none()
    )
    if report is None:
        report = Report(
            sha256=extract.sha256,
            file_path=str(extract.report_path),
            original_filename=extract.report_path.name,
            broker=extract.metadata.get("broker") or "",
            title=extract.metadata.get("title") or extract.report_path.stem,
            page_count=extract.page_count,
            status="extracting",
        )
        session.add(report)
        session.flush()

    report.page_count = extract.page_count
    # 清旧 page 行
    session.query(Page).filter(Page.report_id == report.id).delete()
    session.flush()

    for p in extract.pages:
        session.add(
            Page(
                report_id=report.id,
                page_no=p.page_no,
                text=p.text,
                ocr_used=p.ocr_used,
                ocr_engine=p.ocr_engine,
                char_count=p.char_count,
            )
        )
    session.flush()

    # 刷新 metadata
    md = extract.metadata or {}
    if md.get("title"):
        report.title = md["title"]
    if md.get("broker"):
        report.broker = md["broker"]
    if md.get("report_date"):
        try:
            y, m, d = md["report_date"].split("-")
            report.report_date = dt.date(int(y), int(m), int(d))
        except Exception:  # noqa: BLE001
            pass
    session.add(report)
    session.commit()
    return report


def _index_block(session: Session) -> str:
    """把现有 KPage 索引拼成给 LLM 看的一段文本。

    只发最近 6 条（按 updated_at 倒序），避免 LLM 在思考阶段过度分析导致
    超 max_tokens。同名标题的另存「见 pages_text」即可。
    """
    rows = (
        session.query(KnowledgePage)
        .order_by(KnowledgePage.updated_at.desc())
        .limit(6)
        .all()
    )
    if not rows:
        return "(空)"
    blocks = []
    for kp in rows:
        blocks.append(
            f"- kpage_id={kp.id} | title={kp.title} | "
            f"tags={'/'.join(kp.topic_tags or [])}"
        )
    return "\n".join(blocks)


def _pages_block(report: Report, session: Session) -> str:
    """生成 LLM 看的正文：每页前 N 字符，全文总长封顶。

    MiniMax M3 思考模式 + 长报告会让单次响应触到 max_tokens，故主动封顶：
    - 每页 1500 字符（够覆盖主线、首尾页）
    - 全文 8000 字符封顶（够模型写 JSON）
    """
    pages = session.query(Page).filter(Page.report_id == report.id).order_by(Page.page_no).all()
    PER_PAGE = 1000
    TOTAL_CAP = 5000
    out: list[str] = []
    used = 0
    for p in pages:
        if used >= TOTAL_CAP:
            break
        budget = min(PER_PAGE, TOTAL_CAP - used)
        out.append(f"--- p.{p.page_no} ---")
        chunk = (p.text or "")[:budget]
        out.append(chunk)
        used += len(chunk)
    return "\n".join(out)


def _compile_with_llm(session: Session, report: Report, extract: ExtractResult) -> None:
    """调 LLM，解析 JSON，并落库。

    失败：抛回；外层标记 pending_llm_retry。
    """
    sys_msg = behavior_prelude() + "\n\n" + _ingest_prompt()
    pages_text = _pages_block(report, session)
    user_msg = (
        f"报告元数据：\n"
        f"- original_filename: {report.original_filename}\n"
        f"- broker: {report.broker}\n"
        f"- title: {report.title}\n"
        f"- report_date: {report.report_date.isoformat() if report.report_date else ''}\n"
        f"- page_count: {report.page_count}\n\n"
        f"现有 KPage 索引（最近 6 条）：\n{_index_block(session)}\n\n"
        f"报告正文（按页）：\n{pages_text}\n\n"
        "请按上面 schema 输出 JSON。"
    )

    resp = chat_with_fallback_sync(
        [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ],
        # 长报告/节选 + MiniMax M3 思考模式：max_tokens 给足；
        # 不强制 json_object 模式（部分 provider 在该模式下行为不稳），
        # 我们的 _safe_parse_json 宽容解析 ```` 围栏 + `` 思考块。
        options=LLMOptions(max_tokens=16384),
    )

    # 调试：记 finish_reason 与长度
    log.info(
        "ingest LLM resp: finish_reason=%s len=%d",
        getattr(resp, "stop_reason", "?"), len(resp.content),
    )

    payload = _safe_parse_json(resp.content)
    if payload is None:
        # 不截断，存完整响应供调试；file 长度截 4KB 防爆
        body_dump = resp.content[:4000]
        log.error("INGEST PARSE FAILED — stop_reason=%s\n%s",
                  getattr(resp, "stop_reason", "?"), body_dump)
        raise ValueError(
            f"LLM 输出无法解析为 JSON（stop_reason={getattr(resp,'stop_reason','?')}, "
            f"len={len(resp.content)}）：\n{body_dump}"
        )

    action = payload.get("action", "noop")
    if action == "noop":
        log.info("LLM 决定 noop：%s", payload.get("reasoning", ""))
        return

    sections = payload.get("sections") or []
    if not sections:
        raise ValueError("LLM 输出缺少 sections")

    existing_id = payload.get("existing_kpage_id")
    kpage = None
    if action == "append_to" and existing_id:
        kpage = session.query(KnowledgePage).get(int(existing_id))
    # 程序侧 dedup 保险：LLM 在大 index 上容易"撞名"重复建页
    # 即便 action='create'，若标题已撞了任意一张同 type 的 KPage，自动改 append_to
    if kpage is None and action == "create":
        similar = _find_similar_kpage(
            session,
            payload.get("kpage_title", ""),
            payload.get("kpage_type") or "concept",
            source_filename=report.original_filename,
        )
        if similar is not None:
            log.warning(
                "LLM 决定 create，但标题「%s」撞已有 kpage_id=%d（来源 %s），自动改为 append_to",
                payload.get("kpage_title"), similar.id, similar.source_filename or "?",
            )
            kpage = similar
            action = "append_to"
            existing_id = similar.id
    if kpage is None:
        kpage = KnowledgePage(
            slug=payload.get("kpage_slug") or _slugify(payload.get("kpage_title", "untitled")),
            title=payload.get("kpage_title", "未命名知识页"),
            brief=payload.get("kpage_brief", ""),
            topic_tags=payload.get("topic_tags") or [],
            type=payload.get("kpage_type") or "concept",
            version=1,
        )
        session.add(kpage)
        session.flush()
    else:
        kpage.version = (kpage.version or 1) + 1
        kpage.updated_at = dt.datetime.now(dt.UTC)
        # 若 payload 提供了 type，append 时尊重
        if payload.get("kpage_type"):
            kpage.type = payload["kpage_type"]
        # 把当前报告关联到这张 KPage，便于将来溯源
        if not kpage.source_filename:
            kpage.source_filename = report.original_filename

    # 准备 resolver
    resolver = _build_resolver(session)

    for idx, sec in enumerate(sections):
        heading = sec.get("heading", "").strip()
        body_md = sec.get("body_md", "").strip()
        if not body_md:
            continue
        section_row = KPageSection(
            kpage_id=kpage.id,
            order_idx=idx,
            heading=heading,
            body_md=body_md,
        )
        session.add(section_row)
        session.flush()

        # 验证、落 Citation
        verified = cite_svc.verify_body_md(body_md, resolver=resolver)
        for v in verified:
            if v.report_id is None:
                # 文件名漂移：保留 verbatim token，进 needs_review 通道
                log.warning("无法解析 token: %s", v.ref.filename)
                continue
            session.add(
                Citation(
                    section_id=section_row.id,
                    report_id=v.report_id,
                    page_no=v.ref.page_no,
                    quote=v.ref.preceding_text,
                    quote_hash=v.ref.quote_hash(),
                    span_start=v.ref.span_start,
                    span_end=v.ref.span_end,
                )
            )

    session.add(kpage)
    session.commit()

    # ---- 落 Markdown：每个 KPage 同步写到 `wiki/<type>/<slug>.md' ----
    try:
        wiki_export.write_kpage(kpage, kpage.sections)
        # 来源摘要页（每份材料一份；write_source_page 内部按标题去重）
        topic_titles = [s.heading for s in kpage.sections if s.heading]
        wiki_export.write_source_page(
            source_filename=report.original_filename,
            title=report.title or report.original_filename,
            source_topics=[f"{kpage.title}#{s}" for s in topic_titles],
            source_summary=kpage.brief or "（自动摘要）",
            source_date=report.report_date.isoformat() if report.report_date else "",
            file_relative_path=f"raw/{report.original_filename}",
        )
        # 来源摘要页在 SQLite 里也存一份（type=source）便于检索
        src_path = wiki_export.page_path("source", _slugify(report.title or report.original_filename))
        if src_path.exists():
            from app.models.orm import KnowledgePage as _KP
            existing_src = session.query(_KP).filter(
                _KP.type == "source",
                _KP.slug == _slugify(report.title or report.original_filename),
            ).one_or_none()
            if existing_src is None:
                src_kp = KnowledgePage(
                    slug=_slugify(report.title or report.original_filename),
                    title=report.title or report.original_filename,
                    brief=kpage.brief or "（自动摘要）",
                    topic_tags=[],
                    type="source",
                    source_filename=report.original_filename,
                    version=1,
                )
                session.add(src_kp)
                session.commit()
    except Exception:  # noqa: BLE001
        log.exception("Markdown 导出失败（不影响 SQLite 落库）")


def _build_resolver(session: Session) -> cite_svc.FilenameResolver:
    from app.models.orm import Page, Report as R

    rows = session.query(R).all()
    report_index = []
    for r in rows:
        pages = session.query(Page).filter(Page.report_id == r.id).all()
        pages_dict = {p.page_no: p.text for p in pages}
        report_index.append((r.id, r.original_filename, pages_dict))
    return cite_svc.FilenameResolver.build(report_index)


_SLUG_RE = re.compile(r"[^\w一-鿿]+", flags=re.UNICODE)


def _norm_title(s: str) -> str:
    """把标题归一用于跨 KPage 去重：去空白 / 中英标点，转小写。"""
    if not s:
        return ""
    import re as _re
    s = _re.sub(r"[\s\-_　·・、，,。.：:()（）【】\[\]]+", "", str(s))
    return s.lower()


def _find_similar_kpage(session, title: str, type_: str, source_filename: str | None = None) -> Optional[KnowledgePage]:
    """给定新标题 + type，找现有 KPage 中可能就是要合并的。

    匹配优先级：
      1) 完全相同 source_filename → 同一份报告，必中
      2) 归一化标题相等 → 必中
      3) 标题互相包含（A 包 B 或 B 包 A） → 命中
    """
    if not title:
        return None
    if source_filename:
        hit = (
            session.query(KnowledgePage)
            .filter(KnowledgePage.source_filename == source_filename)
            .filter(KnowledgePage.type != "source")
            .first()
        )
        if hit is not None:
            return hit
    norm_new = _norm_title(title)
    cand = (
        session.query(KnowledgePage)
        .filter(KnowledgePage.type == type_)
        .all()
    )
    for kp in cand:
        if _norm_title(kp.title) == norm_new:
            return kp
    if len(norm_new) >= 6:
        for kp in cand:
            a = _norm_title(kp.title)
            if a and (a in norm_new or norm_new in a):
                return kp
    return None


def _slugify(s: str) -> str:
    if not s:
        return "untitled"
    s = s.strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    return (s or "untitled")[:80]


def _safe_parse_json(text: str) -> Optional[dict]:
    """宽容解析：剥掉 ```` 代码块、`` 思考块，再找 JSON。

    LLM（如 MiniMax-M3）输出形如
    ``<think>...{action: 'create'}...```json\n{...}\n```\n``，
    旧启发式 `find('{') / rfind('}')` 会把思考块里的 `{...}` 当成 JSON 边界、parse 失败。
    现在的策略：
      1) 去掉 ````…```` 代码块包裹
      2) 去掉 ``<think>…</think>`` 块（模型推理痕迹）
      3) 从长到短尝试所有 '{...}' 切片，parse 通过就返回
    """
    if not text:
        return None
    s = text.strip()

    # 1) 去掉 markdown 代码块围栏
    while s.startswith("```"):
        nl = s.find("\n")
        if nl < 0:
            break
        s = s[nl + 1:]
    if s.endswith("```"):
        s = s[: -3].rstrip()

    # 2) 去掉 <think> 块（可能多个）
    s = re.sub(r"<think>.*?</think>\s*", "", s, flags=re.DOTALL).strip()

    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass

    # 3) 从长到短尝试所有 '{...}' 切片
    starts = [i for i, ch in enumerate(s) if ch == "{"]
    ends = [i for i, ch in enumerate(s) if ch == "}"]
    candidates = []
    for i in starts:
        for j in ends:
            if j > i:
                candidates.append((j - i, s[i:j + 1]))
    candidates.sort(reverse=True)
    for _, chunk in candidates[:6]:  # 试前 6 个最长候选
        try:
            return json.loads(chunk)
        except Exception:  # noqa: BLE001
            continue
    return None
