"""FastAPI 路由。
"""
from fastapi import APIRouter

from app.routes.audit_api import router as audit_router
from app.routes.demo_api import router as demo_router
from app.routes.ingest_api import router as ingest_router
from app.routes.qa_api import router as qa_router

api_router = APIRouter(prefix="/api")
api_router.include_router(ingest_router)
api_router.include_router(qa_router)
api_router.include_router(audit_router)
api_router.include_router(demo_router)
