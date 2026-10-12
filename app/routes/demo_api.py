"""演示端点：内置一份示例 PDF + 触发 ingest 全流程。

不依赖外网；写作 / 渲染 / 抽取 / LLM 编译都跑在本机。
当用户没配 LLM API key 时同样能跑：抽取落库成功，状态会停在 `pending_llm_retry`。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.models.db import get_session
from app.services.ingest import ingest_pdf_path

log = logging.getLogger(__name__)
router = APIRouter(tags=["demo"])


SAMPLE_PDF_FILENAME = "招商证券_2026Q1策略.pdf"
SAMPLE_TITLE = "招商证券 2026 Q1 A 股策略：科技 + 高股息双主线"


# demo PDF 生成器委托给 scripts/gen_demo_pdf.py，保持单一权威。
# 旧内联实现（含 bug：未注册 CJK 字体 → 抽出来全是 dots）
# 已删除，避免和 scripts/gen_demo_pdf.py 双源不一致。
try:
    from scripts.gen_demo_pdf import main as _build_sample_pdf  # type: ignore
except ImportError:  # 兼容源码直跑（scripts 不在 sys.path）
    import sys as _sys
    from pathlib import Path as _P
    _SCRIPTS = _P(__file__).resolve().parent.parent / "scripts"
    if str(_SCRIPTS) not in _sys.path:
        _sys.path.insert(0, str(_SCRIPTS))
    from gen_demo_pdf import main as _build_sample_pdf  # type: ignore


@router.post("/demo", summary="导入示例研报")
def demo_import(session: Session = Depends(get_session)) -> JSONResponse:
    """生成示例 PDF → ingest。返回 report_id 与 status 让前端给出反馈。"""
    pdf_dir = settings.wiki_data_dir / "reports"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / SAMPLE_PDF_FILENAME

    # 若先前已经做过示例，复用（避免点一次再生成）
    from sqlalchemy import select
    from app.models.orm import Report
    existing = session.execute(
        select(Report).where(Report.original_filename == SAMPLE_PDF_FILENAME)
    ).scalar_one_or_none()
    if existing and existing.status == "compiled":
        return JSONResponse({
            "ok": True,
            "already": True,
            "report_id": existing.id,
            "status": existing.status,
            "message": "示例研报已存在；已为你打开相关知识页。",
        })

    if not pdf_path.exists():
        _build_sample_pdf(pdf_path)

    if not pdf_path.exists():
        raise HTTPException(status_code=500, detail="示例 PDF 生成失败")

    try:
        report = ingest_pdf_path(pdf_path, session=session)
    except Exception as e:  # noqa: BLE001
        log.exception("示例 ingest 失败")
        raise HTTPException(status_code=500, detail=f"示例摄入失败: {e}")

    return JSONResponse({
        "ok": True,
        "already": False,
        "report_id": report.id,
        "status": report.status,
        "page_count": report.page_count,
        "message": "示例摄入完成（无 LLM key 时停在 pending_llm_retry）",
    })
