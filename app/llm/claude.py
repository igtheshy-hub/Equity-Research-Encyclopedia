"""Claude（Anthropic）实现。

默认 provider。系统提示词从 messages 首条 role=system 读；如缺则内部不注入 system。
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from app.config import settings
from app.llm.base import LLMClient, LLMMessage, LLMOptions, LLMResponse


SYSTEM_SEP = "\n\n[仅当你被明确要求输出 JSON 时，才用 markdown 代码块包裹 JSON；其他场景默认给 Markdown。]"


class ClaudeClient:
    name = "claude"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or settings.anthropic_api_key
        self.base_url = base_url or settings.anthropic_base_url
        self._client = None  # lazy init

    def _sdk(self):
        if self._client is None:
            import anthropic  # 延后导入，便于没装 SDK 时也能 import 模块

            self._client = anthropic.AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    @staticmethod
    def _split_messages(messages: list[LLMMessage]) -> tuple[str, list[dict]]:
        """Anthropic 把 system 单独传；从 messages 里抽出来。"""
        system_parts: list[str] = []
        rest: list[dict] = []
        for m in messages:
            if m.role == "system":
                system_parts.append(m.content)
            else:
                rest.append({"role": m.role, "content": m.content})
        return ("\n\n".join(system_parts) + SYSTEM_SEP).strip(), rest

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        opts = options or LLMOptions()
        system, rest = self._split_messages(messages)
        kwargs: dict = {
            "model": opts.model or settings.llm_default_model,
            "max_tokens": opts.max_tokens,
            "messages": rest,
        }
        if system:
            kwargs["system"] = system
        if opts.response_format_json:
            kwargs["system"] = (
                (kwargs.get("system") or "") + "\n\n请严格输出 JSON，不要任何额外文本或代码块包裹。"
            )

        # Anthropic SDK ≥1.x 把 temperature/ top_p 等放在 extra_body 中
        # （顶层 messages.create 不再接受 temperature 参数）
        if opts.temperature is not None:
            kwargs["extra_body"] = {**kwargs.get("extra_body", {}), "temperature": opts.temperature}

        client = self._sdk()
        resp = await client.messages.create(**kwargs)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return LLMResponse(
            content=text,
            model=resp.model,
            stop_reason=resp.stop_reason or "",
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        *,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[str]:
        opts = options or LLMOptions()
        system, rest = self._split_messages(messages)
        kwargs: dict = {
            "model": opts.model or settings.llm_default_model,
            "max_tokens": opts.max_tokens,
            "messages": rest,
        }
        if system:
            kwargs["system"] = system

        if opts.temperature is not None:
            kwargs["extra_body"] = {**kwargs.get("extra_body", {}), "temperature": opts.temperature}

        client = self._sdk()
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    yield text
            await asyncio.sleep(0)

    # 方便离线 / 测试
    @staticmethod
    def offline_response(text: str, model: str = "claude-offline") -> LLMResponse:
        return LLMResponse(content=text, model=model, stop_reason="end_turn")
