"""提示词加载器。

每个 .md 文件就是一个独立提示词。运行期通过 importlib.resources 读，
便于在打包成 zipapp / 改路径后仍能定位。
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """读 `app/prompts/<name>.md`。

    name 不带扩展名。
    """
    pkg = resources.files("app.prompts")
    return (pkg / f"{name}.md").read_text(encoding="utf-8")


def behavior_prelude() -> str:
    return load_prompt("_behavior")


def ingest_prompt() -> str:
    return load_prompt("ingest")


def qa_select_prompt() -> str:
    return load_prompt("qa_select")


def qa_answer_prompt() -> str:
    return load_prompt("qa_answer")


def audit_prompt() -> str:
    return load_prompt("audit")
