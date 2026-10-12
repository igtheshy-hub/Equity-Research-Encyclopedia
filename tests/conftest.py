"""共享 pytest fixture。

关键约束：测试函数不要在文件顶层 `from app... import ...`，
否则 pytest collection 时把这些名字绑定到默认 SQLite 引擎（旧实例），
后续 fixture 怎么 reload 都无法让那些名字指向新的临时 DB。

fixture 的职责：每个测试提供一个临时 SQLite 文件，并把所有 app.* 模块
在 sys.modules 缓存里删掉，强制测试代码本地 `from app...` 取到新模块实例。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def fresh_app(monkeypatch):
    """每个测试用全新临时 SQLite + 全新 app.* 模块实例。

    测试在函数内部使用：
        from app.config import get_settings
        from app.models.db import init_db, session_scope
        ...
    这些 import 会读 sys.modules，发现 fixture 已经重置过，于是拿到新实例。
    """
    d = Path(tempfile.mkdtemp(prefix="research_wiki_test_"))
    monkeypatch.setenv("WIKI_DATA_DIR", str(d))
    monkeypatch.setenv("WIKI_DB_PATH", str(d / "wiki.sqlite"))

    from app.config import get_settings
    get_settings.cache_clear()

    for mod_name in list(sys.modules.keys()):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]

    from app.models.db import Base, engine
    from app.models import orm  # noqa: F401

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    yield d

    shutil.rmtree(d, ignore_errors=True)
    for mod_name in list(sys.modules.keys()):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]


@pytest.fixture
def stub_llm(monkeypatch):
    """按出队顺序返回 stub LLM 响应（替换 chat_with_fallback_sync）。"""
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


@pytest.fixture
def stub_no_llm(monkeypatch):
    """断言 LLM **不应被调用** 的 fixture：QA 在候选为空时应早返回。"""
    import app.llm.router as router_mod

    state = {"called": False}

    def _fake_sync(messages, *, options=None):
        state["called"] = True
        raise AssertionError("LLM 不该被调用（候选为空时直接答'暂无相关记录'）")

    monkeypatch.setattr(router_mod, "chat_with_fallback_sync", _fake_sync)
    return state
