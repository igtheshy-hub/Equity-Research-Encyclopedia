"""全部 ORM 模型。

三层数据：原始 (Report/Page) → 知识 (KnowledgePage/KPageSection/Citation) → 表面交互 (QA/AuditRun/AuditFinding)
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.db import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------------------------------------------------------------------
# 原始层：Report + Page
# ---------------------------------------------------------------------------
class Report(Base):
    """一份券商研报 PDF。"""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    file_path: Mapped[str] = mapped_column(String(1024))
    original_filename: Mapped[str] = mapped_column(String(512))
    broker: Mapped[str] = mapped_column(String(128), index=True, default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    report_date: Mapped[Optional[dt.date]] = mapped_column(index=True, nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    # pending | extracting | compiled | failed
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[Optional[dt.datetime]] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    pages: Mapped[list["Page"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="Page.page_no"
    )


class Page(Base):
    """一份研报的某一页文本。"""

    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("report_id", "page_no", name="uq_pages_report_pageno"),
        Index("ix_pages_report", "report_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"))
    page_no: Mapped[int] = mapped_column(Integer)  # 1-based 物理页号
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False)
    ocr_engine: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    extraction_meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    report: Mapped[Report] = relationship(back_populates="pages")


# ---------------------------------------------------------------------------
# 知识层：KnowledgePage + KPageSection + Citation + KnowledgeLink
# ---------------------------------------------------------------------------
class KnowledgePage(Base):
    """一篇编译后的团队知识页（wiki 表面 `/k/<slug>`）。

    type 字段与 schema/CLAUDE.md §2 对齐：
      company | industry | concept | source
    """

    __tablename__ = "knowledge_pages"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_kpages_slug"),
        Index("ix_kpages_updated", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(256), index=True)
    title: Mapped[str] = mapped_column(String(512))
    brief: Mapped[str] = mapped_column(Text, default="")
    topic_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, default=None)
    # type=source 的页面：original_filename 关联到原始材料，便于追溯
    source_filename: Mapped[Optional[str]] = mapped_column(String(512), nullable=True, default=None)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 访问次数 / 最近访问时间 用于孤立识别
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[Optional[dt.datetime]] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    sections: Mapped[list["KPageSection"]] = relationship(
        back_populates="kpage",
        cascade="all, delete-orphan",
        order_by="KPageSection.order_idx",
    )
    links_src: Mapped[list["KnowledgeLink"]] = relationship(
        foreign_keys="KnowledgeLink.src_kpage_id",
        back_populates="src",
        cascade="all, delete-orphan",
    )
    links_dst: Mapped[list["KnowledgeLink"]] = relationship(
        foreign_keys="KnowledgeLink.dst_kpage_id",
        back_populates="dst",
        cascade="all, delete-orphan",
    )


class KPageSection(Base):
    """知识页内一节。一节一组主题，body_md 内每句尾可带 [来源:file.pdf:p]。"""

    __tablename__ = "kpage_sections"
    __table_args__ = (
        Index("ix_sections_kpage", "kpage_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kpage_id: Mapped[int] = mapped_column(ForeignKey("knowledge_pages.id", ondelete="CASCADE"))
    order_idx: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(String(512), default="")
    body_md: Mapped[str] = mapped_column(Text)

    kpage: Mapped[KnowledgePage] = relationship(back_populates="sections")
    citations: Mapped[list["Citation"]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )


class Citation(Base):
    """把一句话钉到 (report_id, page_no, quote)。

    quote 是该页 Page.text 里的一段原文，验证时用模糊子串比对。

    注：一个 Section 内的一个 quote 可能被多次引用；这里允许同一 (section, report_id, page_no, quote_hash) 多行存在，
    但为去重保留 quote_hash 字段。MVP 阶段简化：每个 quote 一行。
    """

    __tablename__ = "citations"
    __table_args__ = (
        Index("ix_citations_section", "section_id"),
        Index("ix_citations_report_page", "report_id", "page_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    section_id: Mapped[int] = mapped_column(ForeignKey("kpage_sections.id", ondelete="CASCADE"))
    report_id: Mapped[int] = mapped_column(ForeignKey("reports.id", ondelete="CASCADE"))
    page_no: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)  # 原文引用；模糊比对时这就是金标准
    quote_hash: Mapped[str] = mapped_column(String(64), index=True)
    span_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 在 body_md 中字符偏移
    span_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    section: Mapped[KPageSection] = relationship(back_populates="citations")


class KnowledgeLink(Base):
    """知识页之间的语义链接：related / contradicts / supersedes。"""

    __tablename__ = "knowledge_links"
    __table_args__ = (
        UniqueConstraint("src_kpage_id", "dst_kpage_id", "kind", name="uq_klinks_triplet"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    src_kpage_id: Mapped[int] = mapped_column(ForeignKey("knowledge_pages.id", ondelete="CASCADE"))
    dst_kpage_id: Mapped[int] = mapped_column(ForeignKey("knowledge_pages.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))  # related | contradicts | supersedes
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    src: Mapped[KnowledgePage] = relationship(foreign_keys=[src_kpage_id], back_populates="links_src")
    dst: Mapped[KnowledgePage] = relationship(foreign_keys=[dst_kpage_id], back_populates="links_dst")


# ---------------------------------------------------------------------------
# 表面层：QA 历史
# ---------------------------------------------------------------------------
class QA(Base):
    """一条问答历史。

    selected_pages 存 kpage_id 列表（用户问题的相关页）。
    answer_md 存模型最终回答。
    verification 存逐句引用校验结果。
    """

    __tablename__ = "qa_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(Text)
    selected_pages: Mapped[list[int]] = mapped_column(JSON, default=list)  # list of kpage_id
    answer_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    citations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    asked_by: Mapped[str] = mapped_column(String(64), default="anonymous")
    asked_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


# ---------------------------------------------------------------------------
# 审校层：AuditRun + AuditFinding
# ---------------------------------------------------------------------------
class AuditRun(Base):
    """一次审校运行。"""

    __tablename__ = "audit_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    # running | succeeded | failed
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    findings: Mapped[list["AuditFinding"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="AuditFinding.id"
    )


class AuditFinding(Base):
    """一次审校运行中的一条发现。

    kind ∈ contradiction | outdated | uningested | isolated
    contradiction / outdated 必须 quote_a / quote_b 同时有；程序侧做硬校验
    """

    __tablename__ = "audit_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("audit_runs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="medium")  # low | medium | high
    summary: Mapped[str] = mapped_column(Text)
    topic: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    quote_a: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cite_a: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    quote_b: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cite_b: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    kpage_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    report_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    verification_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    run: Mapped[AuditRun] = relationship(back_populates="findings")
