"""智谱 GLM 提供方（与 Igshy 主仓默认一致）。

OpenAI 兼容接口：`https://open.bigmodel.cn/api/paas/v4/chat/completions`
通过 `.env` 中的 `ZHIPU_API_KEY` 鉴权。
模型默认 `glm-4.6`，可在 `.env` 用 `ZHIPU_MODEL=` 覆盖。
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from app.config import settings
from app.llm.base import LLMClient, LLMMessage, LLMOptions, LLMResponse

log = logging.getLogger(__name__)

_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


class ZhipuClient:
    name = "zhipu"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None) -> None:
        self.api_key = api_key or settings.zhipu_api_key
        self.base_url = base_url or settings.zhipu_base_url or _BASE_URL
        self.model = model or settings.zhipu_model or "glm-4.6"
        if not self.api_key:
            log.warning("ZHIPU_API_KEY 未配置；调 ZhipuClient 会失败。")

    async def chat(
        self,
        messages: list[LLMMessage],
        *,
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        opts = options or LLMOptions()
        import httpx  # 延后 import

        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": opts.temperature if opts.temperature is not None else 0.2,
        }
        if opts.max_tokens:
            payload["max_tokens"] = opts.max_tokens
        if opts.response_format_json:
            payload["response_format"] = {"type": "json_object"}
        # 关闭深度思考以免超时
        payload["thinking"] = {"type": "disabled"}

        timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"智谱 API 错误 {resp.status_code}: {str(data)[:300]}")

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"智谱响应结构异常: {e}; body={str(data)[:200]}")

        return LLMResponse(
            content=content,
            model=self.model,
            stop_reason=choice.get("finish_reason", ""),
            usage=data.get("usage") or {},
            raw=data if isinstance(data, dict) else None,
        )

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        *,
        options: LLMOptions | None = None,
    ) -> AsyncIterator[str]:
        """流式输出当前用最小可用：非流式一次返回整个 content，分块 yield。"""
        opts = options or LLMOptions()
        resp = await self.chat(messages, options=opts)
        yield resp.content or ""
        await asyncio.sleep(0)
