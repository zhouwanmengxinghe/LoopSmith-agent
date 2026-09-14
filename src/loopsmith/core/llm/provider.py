from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import anthropic

from loopsmith.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from loopsmith.core.events.bus import EventBus
from loopsmith.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)
_DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnthropicProvider:
    # 初始化 Anthropic 客户端；client 可在测试时注入以跳过 API key 检查
    def __init__(
        self,
        model: str,
        client: Any = None,
        *,
        base_url: str | None = None,
    ) -> None:
        if base_url is None and model.startswith("deepseek-"):
            base_url = _DEEPSEEK_ANTHROPIC_BASE_URL
        self._is_deepseek = model.startswith("deepseek-") or (
            base_url is not None and "api.deepseek.com" in base_url
        )

        if client is None:
            if self._is_deepseek:
                api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get(
                    "ANTHROPIC_API_KEY"
                )
            else:
                api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                expected = (
                    "DEEPSEEK_API_KEY or ANTHROPIC_API_KEY"
                    if self._is_deepseek
                    else "ANTHROPIC_API_KEY"
                )
                raise SystemExit(f"{expected} not set")
            client_kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url is not None:
                client_kwargs["base_url"] = base_url
            self._client: Any = anthropic.AsyncAnthropic(**client_kwargs)
        else:
            self._client = client
        self._model = model

    # 流式调用 Anthropic API，逐 token 发布事件并返回 LlmResponse
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )

        system: list[dict[str, object]] = [
            {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        ]

        tools: list[dict[str, object]] = list(tool_schemas)
        if tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools = tools[:-1] + [last]

        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }
        if self._is_deepseek:
            # 当前循环没有保存 reasoning block；关闭思考模式可保证多轮工具调用兼容。
            kwargs["extra_body"] = {"reasoning": {"effort": "none"}}
        if tools:
            kwargs["tools"] = tools

        text_parts: list[str] = []

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
                text_parts.append(text)
            final_message = await stream.get_final_message()

        usage = final_message.usage
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create: int = getattr(usage, "cache_creation_input_tokens", 0) or 0

        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                ts=_now(),
            )
        )

        tool_calls: list[ToolCallBlock] = []
        for block in final_message.content:
            if block.type == "tool_use":
                tool_calls.append(
                    ToolCallBlock(id=block.id, name=block.name, input=dict(block.input))
                )

        return LlmResponse(
            stop_reason=final_message.stop_reason or "end_turn",
            tool_calls=tool_calls,
            text="".join(text_parts),
            usage=UsageStats(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
            ),
        )
