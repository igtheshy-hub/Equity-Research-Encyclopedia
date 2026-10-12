"""Pydantic 模型：HTTP 入参出参。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class IngestUploadResponse(BaseModel):
    report_id: int
    sha256: str
    status: str
    page_count: int
    message: str = ""


class IngestStatusResponse(BaseModel):
    report_id: int
    status: str
    page_count: int
    title: str
    error: Optional[str] = None


class QARequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    asked_by: str = Field(default="anonymous", max_length=64)


class QAResponse(BaseModel):
    question: str
    answer_md: str
    selected_kpage_ids: list[int]
    out_of_scope: bool
    verification: dict
    citations: list[dict]


class AuditRunResponse(BaseModel):
    run_id: int
    status: str
    started_at: str
    finished_at: Optional[str] = None
    summary: dict
    error: Optional[str] = None
