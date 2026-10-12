"""FastAPI 依赖注入。"""
from __future__ import annotations

from app.models.db import get_session

__all__ = ["get_session"]
