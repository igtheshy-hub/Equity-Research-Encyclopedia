"""LLM 客户端抽象。

所有具体实现（Claude / OpenAI 兼容）都暴露 chat() 和 stream_chat() 两个方法，
返回结构化 dict，包括 content / usage / model / stop_reason。

router.py 负责选谁、降级、限流。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass
class LLMMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class LLMResponse:
    content: str
    model: str
    stop_reason: str = ""
    usage: dict = field(default_factory=dict)  # input_tokens / output_tokens
    raw: dict | None = None


@dataclass
class LLMOptions:
    model: str | None = None  # 不传则用 provider 默认
    max_tokens: int = 4096
    temperature: float = 0.0
    response_format_json: bool = False  # 提示 provider 让模型倾向输出 JSON
    timeout_s: float = 120.0


class LLMClient(Protocol):
    """所有 LLM provider 的统一接口。"""

    name: str

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        options: LLMOptions | None = None,
    ) -> LLMResponse: ...

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        *,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[str]: ...
