"""模型路由：根据 settings 选 provider；支持主→备降级。

同时提供 sync 与 async 两套入口：
- chat_with_fallback(...)
- chat_with_fallback_sync(...)   ← 服务层（同步）和 FastAPI sync 端点用

`chat_with_fallback_sync` 关键点：
- 若**没有**正在跑的 event loop（线程池 worker 或纯脚本），直接 asyncio.run
- 若**有**正在跑的 loop（FastAPI async 中、或 async 端点内），把 asyncio.run 推到一个独立线程去执行
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Optional

from app.config import settings
from app.llm.base import LLMClient, LLMMessage, LLMOptions, LLMResponse
from app.llm.claude import ClaudeClient
from app.llm.openai_compat import OpenAICompatClient
from app.llm.zhipu import ZhipuClient

log = logging.getLogger(__name__)

_sync_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _get_sync_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _sync_executor
    if _sync_executor is None:
        _sync_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="wiki-llm-sync",
        )
    return _sync_executor


def make_client(provider: str | None = None) -> LLMClient:
    name = (provider or settings.llm_provider or "claude").lower()
    if name == "claude":
        return ClaudeClient()
    # OpenAI 兼容协议：原生 OpenAI / DeepSeek / 通义 / Kimi / MiniMax 等都吃同一套
    if name in {"openai", "openai_compat", "deepseek", "qwen", "kimi", "minimax"}:
        return OpenAICompatClient()
    if name in {"zhipu", "glm", "bigmodel"}:
        return ZhipuClient()
    raise ValueError(f"未知 LLM provider: {name}")


# 单例；首次解析时建立
_default_client: LLMClient | None = None


def get_default_client() -> LLMClient:
    global _default_client
    if _default_client is None:
        _default_client = make_client()
    return _default_client


def _coerce(messages):
    """把 dict 形态消息统一转 LLMMessage 实例，调用方写起来更顺手。"""
    out = []
    for m in messages:
        if isinstance(m, LLMMessage):
            out.append(m)
        elif isinstance(m, dict):
            role = m.get("role") or "user"
            content = m.get("content") or ""
            out.append(LLMMessage(role=role, content=content))
        else:
            raise TypeError(f"消息必须是 LLMMessage 或 dict，得到 {type(m).__name__}")
    return out


async def chat_with_fallback(
    messages,
    *,
    options: LLMOptions | None = None,
    fallback_to_alt_provider: bool = True,
) -> LLMResponse:
    """主 provider 失败时降级到备用 provider / 备用模型。async。

    messages 可以是 LLMMessage 列表，也可以是 [{"role":..., "content":...}] 列表。
    """
    messages = _coerce(messages)
    primary = get_default_client()
    try:
        return await primary.chat(messages, options=options)
    except Exception as e:  # noqa: BLE001
        log.warning("主 LLM 调用失败 (%s)：%s", primary.name, e)

        if fallback_to_alt_provider:
            alt_name = "openai_compat" if primary.name == "claude" else "claude"
            try:
                alt = make_client(alt_name)
                log.info("降级到 %s", alt_name)
                return await alt.chat(messages, options=options)
            except Exception as e2:  # noqa: BLE001
                log.warning("降级 provider 也失败 (%s)：%s", alt_name, e2)

        if options and options.model is None:
            from copy import replace

            fb_opts = replace(options, model=settings.llm_fallback_model)
            try:
                return await primary.chat(messages, options=fb_opts)
            except Exception as e3:  # noqa: BLE001
                log.warning("主 provider 备用模型也失败：%s", e3)

        raise


def chat_with_fallback_sync(
    messages,
    *,
    options: LLMOptions | None = None,
    fallback_to_alt_provider: bool = True,
) -> LLMResponse:
    """同步入口：服务层 / FastAPI sync 端点都用这个。

    messages 可以是 LLMMessage 列表，也可以是 [{"role":..., "content":...}] 列表。
    自动检测是否有正在跑的 event loop：
    - 有 → 把 `asyncio.run(...)` 推到一个独立线程里跑
    - 无 → 直接 `asyncio.run(...)`
    """
    async def _run():
        return await chat_with_fallback(
            messages,
            options=options,
            fallback_to_alt_provider=fallback_to_alt_provider,
        )

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        return asyncio.run(_run())

    fut = _get_sync_executor().submit(asyncio.run, _run())
    return fut.result()
