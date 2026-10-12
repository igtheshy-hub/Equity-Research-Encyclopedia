"""OpenAI 兼容实现——DeepSeek / 通义千问 / Kimi / 本地 vllm 都吃同一接口。"""
from __future__ import annotations

import json
from typing import AsyncIterator

from app.config import settings
from app.llm.base import LLMClient, LLMMessage, LLMOptions, LLMResponse


class OpenAICompatClient:
    name = "openai_compat"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = api_key or settings.openai_api_key
        self.base_url = base_url or settings.openai_base_url

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        from openai import AsyncOpenAI

        opts = options or LLMOptions()
        model = opts.model or settings.openai_model
        msgs = [{"role": m.role, "content": m.content} for m in messages]
        kwargs: dict = {
            "model": model,
            "messages": msgs,
            "max_tokens": opts.max_tokens,
            "temperature": opts.temperature,
        }
        if opts.response_format_json:
            kwargs["response_format"] = {"type": "json_object"}

        # 注意：不缓存 AsyncOpenAI 客户端 —— 跨 asyncio.run 复用会触发
        # httpx 的 "Event loop is closed"。每次 chat 新建一次。
        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        try:
            resp = await client.chat.completions.create(**kwargs)
        finally:
            await client.close()
        choice = resp.choices[0]
        return LLMResponse(
            content=choice.message.content or "",
            model=resp.model or model,
            stop_reason=choice.finish_reason or "",
            usage={
                "input_tokens": getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0,
                "output_tokens": getattr(resp.usage, "completion_tokens", 0) if resp.usage else 0,
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
        model = opts.model or settings.openai_model
        msgs = [{"role": m.role, "content": m.content} for m in messages]

        client = self._sdk()
        stream = await client.chat.completions.create(
            model=model,
            messages=msgs,
            max_tokens=opts.max_tokens,
            temperature=opts.temperature,
            stream=True,
        )
        async for chunk in stream:
            for c in chunk.choices or []:
                delta = c.delta.content
                if delta:
                    yield delta

    @staticmethod
    def offline_response(text: str, model: str = "openai-compat-offline") -> LLMResponse:
        return LLMResponse(content=text, model=model, stop_reason="stop")
