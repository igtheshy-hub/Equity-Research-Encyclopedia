"""端到端测试：合成 PDF → 摄入 → 问答 → 审校 全链路。

关键设计：所有 `from app...` 都在测试**函数内**做，确保每次拿到的 module 都
指向 fixture 设置的临时 SQLite（不被 pytest collection 阶段捕获的旧引用污染）。

锚点格式：[[文件名.{pdf|docx}#{pN|paraN}]]（与 schema/CLAUDE.md §1 / Igshy 主仓对齐）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


# 顶部只引用 pytest + 类型；不 import app.* 的具体符号
# ---------------------------------------------------------------------------
# 合成 PDF
# ---------------------------------------------------------------------------
def _write_synthetic_pdf(path: Path, pages: list[str]) -> None:
    import fitz  # pymupdf

    doc = fitz.open()
    for txt in pages:
        page = doc.new_page()
        page.insert_text((72, 72), txt)
    doc.save(path.as_posix())
    doc.close()


@pytest.fixture
def stub_llm(monkeypatch):
    """按出队顺序返回 stub 响应。"""
    import app.llm.router as router_mod

    state = {"queue": []}

    def fake_sync(messages, *, options=None):
        if not state["queue"]:
            raise AssertionError("StubLLM 队列耗尽")
        return type("R", (), {
            "content": state["queue"].pop(0),
            "model": "stub",
            "stop_reason": "end_turn",
        })()

    monkeypatch.setattr(router_mod, "chat_with_fallback_sync", fake_sync)
    return state


# ---------------------------------------------------------------------------
# 1. 摄入：合成 PDF + ingest LLM 响应 → DB 验证
# ---------------------------------------------------------------------------
def test_ingest_end_to_end(fresh_app, stub_llm):
    from app.models.db import init_db, session_scope
    from app.models.orm import Citation, KPageSection, KnowledgePage, Page, Report
    from app.services.ingest import ingest_pdf_path

    pdf = fresh_app / "demo.pdf"
    _write_synthetic_pdf(pdf, [
        "公司 2026 年一季度营收同比+12.5%，归母净利润 50 亿。",
        "公司高端化加速，毛利率提升至 32.4%。",
    ])
    init_db()

    stub_llm["queue"].append(json.dumps({
        "action": "create",
        "kpage_slug": "demo-2026q1",
        "kpage_title": "示例：2026 Q1 业绩",
        "kpage_brief": "由 demo 提炼的演示知识页",
        "topic_tags": ["演示", "业绩"],
        "sections": [
            {
                "heading": "盈利",
                "body_md": "公司 2026 年一季度营收同比+12.5%，归母净利润 50 亿。[[demo.pdf#p1]]",
            },
            {
                "heading": "毛利率",
                "body_md": "公司高端化加速，毛利率提升至 32.4%。[[demo.pdf#p2]]",
            },
        ],
        "reasoning": "演示用 create 分支",
    }, ensure_ascii=False))

    with session_scope() as s:
        report = ingest_pdf_path(pdf, session=s)
        assert report.status == "compiled"
        assert report.page_count == 2
        page_rows = s.query(Page).filter(Page.report_id == report.id).order_by(Page.page_no).all()
        assert [p.page_no for p in page_rows] == [1, 2]
        kp = s.query(KnowledgePage).filter(KnowledgePage.slug == "demo-2026q1").one()
        assert "示例" in kp.title
        secs = s.query(KPageSection).filter(KPageSection.kpage_id == kp.id).order_by(KPageSection.order_idx).all()
        assert len(secs) == 2
        assert secs[0].heading == "盈利"
        cits = s.query(Citation).filter(Citation.section_id.in_([x.id for x in secs])).all()
        assert len(cits) == 2
        assert {c.page_no for c in cits} == {1, 2}


# ---------------------------------------------------------------------------
# 2. 问答
# ---------------------------------------------------------------------------
def _seed_kpage(session, page_text_1: str, page_text_2: str):
    from app.models.orm import (
        KPageSection, KnowledgePage, Page, Report,
    )

    r = Report(
        sha256="sha_demo_qa",
        file_path="/tmp/demo.pdf",
        original_filename="demo.pdf",
        broker="测试",
        title="演示报告",
        page_count=2,
        status="compiled",
        ingested_at=datetime.now(timezone.utc),
    )
    session.add(r); session.flush()
    session.add_all([
        Page(report_id=r.id, page_no=1, text=page_text_1, char_count=len(page_text_1)),
        Page(report_id=r.id, page_no=2, text=page_text_2, char_count=len(page_text_2)),
    ])
    kp = KnowledgePage(
        slug="demo-2026q1",
        title="示例：2026 Q1 业绩",
        brief="由测试样本植入",
        topic_tags=["业绩", "演示"],
    )
    session.add(kp); session.flush()
    session.add_all([
        KPageSection(
            kpage_id=kp.id, order_idx=0, heading="盈利",
            body_md="公司 2026 年一季度营收同比+12.5%。[[demo.pdf#p1]]",
        ),
        KPageSection(
            kpage_id=kp.id, order_idx=1, heading="毛利率",
            body_md="公司高端化加速，毛利率提升至 32.4%。[[demo.pdf#p2]]",
        ),
    ])
    session.commit()
    return kp, r


def test_qa_end_to_end_ok(fresh_app, stub_llm):
    from app.models.db import init_db, session_scope
    from app.models.orm import QA
    from app.services.qa import ask_question

    init_db()
    with session_scope() as s:
        _seed_kpage(
            s,
            page_text_1="公司 2026 年一季度营收同比+12.5%，归母净利润 50 亿。",
            page_text_2="公司高端化加速，毛利率提升至 32.4%。",
        )

    stub_llm["queue"].append(json.dumps({
        "selected": [1], "confidence": "high", "reason": "匹配业绩主题"
    }, ensure_ascii=False))
    stub_llm["queue"].append(
        "公司 2026 年一季度营收同比+12.5%，归母净利润 50 亿。[[demo.pdf#p1]]\n\n"
        "## 引用清单\n- 示例：2026 Q1 业绩（kpage_id=1） — 引自 demo.pdf 第 1 页：营收同比+12.5%。"
    )

    with session_scope() as s:
        result = ask_question("2026 Q1 营收如何？", session=s, asked_by="tester")

    assert not result.out_of_scope
    assert result.selected_kpage_ids == [1]
    assert "[[demo.pdf#p1]]" in result.answer_md
    assert result.verification["tokens_verified"] >= 1

    with session_scope() as s:
        qa = s.query(QA).filter(QA.question == "2026 Q1 营收如何？").first()
        assert qa is not None
        assert qa.answer_md is not None


def test_qa_out_of_scope_no_candidates(fresh_app, stub_llm):
    from app.models.db import init_db, session_scope
    from app.services.qa import ask_question

    init_db()

    with session_scope() as s:
        r = ask_question("随便问问", session=s)

    assert r.out_of_scope
    assert "知识库中暂无" in r.answer_md
    assert stub_llm["queue"] == []  # 不该消耗任何 LLM 调用


# ---------------------------------------------------------------------------
# 3. 审校
# ---------------------------------------------------------------------------
def test_audit_catches_contradiction(fresh_app, stub_llm):
    import json
    from app.models.db import init_db, session_scope
    from app.models.orm import (
        AuditFinding, KPageSection, KnowledgePage, Page, Report,
    )
    from app.services.audit import run_audit

    init_db()

    with session_scope() as s:
        r1 = Report(
            sha256="s1", file_path="/tmp/a.pdf", original_filename="a.pdf",
            broker="测试", title="早期观点", page_count=1, status="compiled",
        )
        r2 = Report(
            sha256="s2", file_path="/tmp/b.pdf", original_filename="b.pdf",
            broker="测试", title="新观点", page_count=1, status="compiled",
        )
        s.add_all([r1, r2]); s.flush()
        s.add_all([
            Page(report_id=r1.id, page_no=1, text="公司白酒业务进入新周期，高端化加速。", char_count=23),
            Page(report_id=r2.id, page_no=1, text="白酒行业去库存压力大，公司高端化减速。", char_count=21),
        ])
        kp1 = KnowledgePage(slug="kp1", title="旧观点", topic_tags=["白酒"])
        kp2 = KnowledgePage(slug="kp2", title="新观点", topic_tags=["白酒"])
        s.add_all([kp1, kp2]); s.flush()
        s.add_all([
            KPageSection(kpage_id=kp1.id, order_idx=0, heading="",
                body_md="公司白酒业务进入新周期，高端化加速。[[a.pdf#p1]]"),
            KPageSection(kpage_id=kp2.id, order_idx=0, heading="",
                body_md="白酒行业去库存压力大，公司高端化减速。[[b.pdf#p1]]"),
        ])
        s.commit()
        kp1_id, kp2_id = kp1.id, kp2.id

    stub_llm["queue"].append(json.dumps({
        "contradictions": [
            {
                "topic": "白酒",
                "claim_a": "公司白酒业务进入新周期，高端化加速",
                "quote_a": "公司白酒业务进入新周期，高端化加速。",
                "cite_a": "a.pdf#p1",
                "claim_b": "白酒行业去库存压力大，公司高端化减速",
                "quote_b": "白酒行业去库存压力大，公司高端化减速。",
                "cite_b": "b.pdf#p1",
                "severity": "high",
                "summary": "对白酒周期的判断完全相反",
            }
        ],
        "outdated": [], "uningested": [], "isolated": [],
    }, ensure_ascii=False))

    with session_scope() as s:
        run = run_audit(s)
        assert run.status == "succeeded"
        findings = s.query(AuditFinding).filter(AuditFinding.run_id == run.id).all()
        assert len(findings) >= 1
        f = next(f for f in findings if f.kind == "contradiction")
        assert f.severity == "high"
        assert "新周期" in (f.quote_a or "")
        assert "减速" in (f.quote_b or "")
        # quote 都在 Page.text 中能找到 → 不应留 verification_note
        assert f.verification_note is None


def test_audit_flags_unverified_quote(fresh_app, stub_llm):
    """构造 2 个同主题 KPage 触发 LLM 比对，喂入故意错位的 quote，程序应标 verification_note。"""
    import json
    from app.models.db import init_db, session_scope
    from app.models.orm import (
        AuditFinding, KPageSection, KnowledgePage, Page, Report,
    )
    from app.services.audit import run_audit

    init_db()
    with session_scope() as s:
        r1 = Report(
            sha256="s3a", file_path="/tmp/c1.pdf", original_filename="c.pdf",
            broker="测试", title="C1", page_count=1, status="compiled",
        )
        r2 = Report(
            sha256="s3b", file_path="/tmp/c2.pdf", original_filename="d.pdf",
            broker="测试", title="C2", page_count=1, status="compiled",
        )
        s.add_all([r1, r2]); s.flush()
        s.add_all([
            Page(report_id=r1.id, page_no=1,
                 text="原油价格 2026 Q1 同比 -8.5%。", char_count=20),
            Page(report_id=r2.id, page_no=1,
                 text="原油预期上行，看多 2026 H2 油价。", char_count=20),
        ])
        kp1 = KnowledgePage(slug="oil-old", title="原油（旧观点）", topic_tags=["原油"])
        kp2 = KnowledgePage(slug="oil-new", title="原油（新观点）", topic_tags=["原油"])
        s.add_all([kp1, kp2]); s.flush()
        s.add_all([
            KPageSection(kpage_id=kp1.id, order_idx=0, heading="",
                body_md="原油价格 2026 Q1 同比 -8.5%。[[c.pdf#p1]]"),
            KPageSection(kpage_id=kp2.id, order_idx=0, heading="",
                body_md="原油预期上行，看多 2026 H2 油价。[[d.pdf#p1]]"),
        ])
        s.commit()
        kp1_id, kp2_id = kp1.id, kp2.id

    # LLM 给出 quote_a 故意写错（数字翻转），正应对应 c.pdf:1 原文
    stub_llm["queue"].append(json.dumps({
        "contradictions": [
            {
                "topic": "原油",
                "claim_a": "原油同比上涨",
                "quote_a": "原油价格同比 +5%。",  # 与 Page c.pdf:1 原文不一致
                "cite_a": "c.pdf#p1",
                "claim_b": "原油同比下跌",
                "quote_b": "原油价格 2026 Q1 同比 -8.5%。",
                "cite_b": "d.pdf#p1",
                "severity": "high",
                "summary": "错位 quote 应被识别",
            }
        ],
        "outdated": [], "uningested": [], "isolated": [],
    }, ensure_ascii=False))

    with session_scope() as s:
        run = run_audit(s)
        assert run.status == "succeeded"
        f = s.query(AuditFinding).filter(AuditFinding.run_id == run.id).first()
        assert f is not None
        # 错位 quote 应被程序标注 verification_note
        assert f.verification_note is not None
        assert "原文比对" in f.verification_note
