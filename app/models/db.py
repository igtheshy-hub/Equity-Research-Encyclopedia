"""SQLAlchemy 引擎与 Base。

MVP 用 SQLite 单文件；将来切 PostgreSQL 仅需替换 engine 的 URL。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """所有 ORM 的基类。"""


def _make_engine() -> Engine:
    """SQLite 需要 check_same_thread=False 以便在 FastAPI worker 线程里用。"""
    eng = create_engine(
        settings.sqlite_url,
        echo=False,
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    # SQLite 外键默认关闭；打开以保关系完整性
    @event.listens_for(eng, "connect")
    def _enable_fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    return eng


engine: Engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    """创建所有表；MVP 不使用 Alembic，靠 ORM 同步建表 + 轻量 ALTER 迁移。"""
    # 必须在 import 所有 ORM 之后调用，避免循环引用；
    # 这行由 main.py 里显式触发。
    from app.models import orm  # noqa: F401

    Base.metadata.create_all(bind=engine)

    # ----- 轻量迁移（仅 SQLite，Alembic 后续替换） -----
    with engine.begin() as conn:
        # KnowledgePage 新增 type / source_filename
        cols = [row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info(knowledge_pages)"
        ).fetchall()]
        if "type" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE knowledge_pages ADD COLUMN type VARCHAR(32)"
            )
        if "source_filename" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE knowledge_pages ADD COLUMN source_filename VARCHAR(512)"
            )


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务作用域：with 块内任意异常即回滚。"""
    sess = SessionLocal()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖注入专用。"""
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
