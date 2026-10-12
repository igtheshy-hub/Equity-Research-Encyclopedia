"""FastAPI 入口。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.models.db import init_db
from app.routes import api_router
from app.routes.pages import router as pages_router
from app.services.audit_runner import start_scheduler, stop_scheduler


logging.basicConfig(
    level=settings.wiki_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("数据库已就绪：%s", settings.wiki_db_path)
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


app = FastAPI(
    title="Research Wiki — 研报团队百科",
    description="把券商研报沉淀为句句标页码的可审可查团队知识库。",
    version="0.1.0",
    lifespan=lifespan,
)

# 静态资源
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 路由
app.include_router(pages_router)
app.include_router(api_router)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


def run() -> None:
    """`python -m app.main` / `research-wiki` 入口。"""
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.wiki_host,
        port=settings.wiki_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
