from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from loopsmith.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from loopsmith.core.events.bus import EventBus
from loopsmith.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

HKU_BASE_URL = "https://api.hku.hk/foundry/student/models"


# 将现有 Anthropic 格式消息转换为 HKU Chat Completions 消息
def convert_messages(messages: list[dict[str, object]], system: str | None) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if system:
        output.append({"role": "system", "content": system})
    for message in messages:
        role, content = message["role"], message.get("content", "")
        if isinstance(content, str):
            output.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("Unsupported message content")
        texts, calls, results, reasoning = [], [], [], []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                texts.append(block["text"])
            elif kind == "tool_use":
                calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"], ensure_ascii=False),
                        },
                    }
                )
            elif kind == "tool_result":
                results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": block["content"],
                    }
                )
            elif kind == "thinking":
                reasoning.append(block.get("thinking", ""))
            else:
                raise ValueError(f"Unsupported message block: {kind}")
        if texts or calls:
            converted: dict[str, Any] = {"role": role, "content": "\n".join(texts) or None}
            if calls:
                converted["tool_calls"] = calls
            if reasoning:
                converted["reasoning_content"] = "".join(reasoning)
            output.append(converted)
        output.extend(results)
    return output


# 累积流式工具字段；null 代表无增量，其他非字符串值必须拒绝
def _fragment(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Invalid SSE string fragment")
    return value


class HkuProvider:
    # 校验目的主机并保存可注入测试传输，密钥只在请求时读取
    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        max_output_tokens: int = 4096,
        context_window: int = 0,
        timeout_s: int = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or HKU_BASE_URL).rstrip("/")
        parsed = urlsplit(self._base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.hku.hk"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in (None, 443)
        ):
            raise ValueError("HKU credentials may only be sent to https://api.hku.hk")
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._context_window = context_window
        self._timeout = timeout_s
        self._transport = transport

    # 请求 HKU 流式接口并转换成现有循环响应；不自动重试以避免隐藏额度消耗
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        key = os.environ.get("HKU_API_KEY")
        if not key:
            raise ValueError("HKU_API_KEY not set")
        body: dict[str, Any] = {
            "model": self._model,
            "messages": convert_messages(messages, system),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self._max_output_tokens,
        }
        if tool_schemas:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tool_schemas
            ]
        await bus.publish(
            LlmModelSelectedEvent(
                run_id=run_id,
                model=self._model,
                strategy="static",
                ts=_now(),
            )
        )
        text: list[str] = []
        reasoning_text: list[str] = []
        tools: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage: dict[str, Any] | None = None
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST",
                self._base_url + "/chat/completions",
                json=body,
                headers={"api-key": key, "Accept": "text/event-stream"},
            ) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"HKU API HTTP {response.status_code}; no automatic retry")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("error"):
                        raise RuntimeError("HKU API returned a stream error")
                    if chunk.get("usage") is not None:
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        if choice.get("index", 0) != 0:
                            continue
                        finish = choice.get("finish_reason") or finish
                        delta = choice.get("delta") or {}
                        token = _fragment(delta.get("content"))
                        reasoning_text.append(_fragment(delta.get("reasoning_content")))
                        if token:
                            text.append(token)
                            await bus.publish(LlmTokenEvent(run_id=run_id, token=token, ts=_now()))
                        for call in delta.get("tool_calls") or []:
                            index = call.get("index")
                            if type(index) is not int or index < 0:
                                raise ValueError("Invalid tool call index")
                            target = tools.setdefault(index, {"id": "", "name": "", "args": ""})
                            target["id"] += _fragment(call.get("id"))
                            function = call.get("function")
                            if function is not None:
                                target["name"] += _fragment(function.get("name"))
                                target["args"] += _fragment(function.get("arguments"))
        if finish not in ("stop", "tool_calls", "length"):
            raise RuntimeError("HKU response incomplete or blocked; no valid finish_reason")
        parsed_tools = []
        # 长度截断的工具参数不可执行，也不可被误记为任务完成
        if finish != "length":
            for _, call in sorted(tools.items()):
                if not call["id"] or not call["name"]:
                    raise ValueError("Incomplete HKU tool call")
                arguments = json.loads(call["args"] or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("Tool arguments must be an object")
                parsed_tools.append(ToolCallBlock(call["id"], call["name"], arguments))
            if bool(parsed_tools) != (finish == "tool_calls"):
                raise ValueError("HKU tool calls do not match finish_reason")
        stats = None
        if usage is not None:
            prompt = int(usage["prompt_tokens"])
            completion = int(usage["completion_tokens"])
            cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
            stats = UsageStats(
                input_tokens=prompt,
                output_tokens=completion,
                cache_read_input_tokens=cached,
                context_pct=prompt / self._context_window if self._context_window else 0.0,
            )
            await bus.publish(
                LlmUsageEvent(
                    run_id=run_id,
                    input_tokens=prompt,
                    output_tokens=completion,
                    cache_read_input_tokens=cached,
                    cache_creation_input_tokens=0,
                    context_pct=stats.context_pct,
                    ts=_now(),
                )
            )
        return LlmResponse(
            stop_reason={"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}[
                finish
            ],
            text="".join(text),
            tool_calls=parsed_tools,
            usage=stats,
            thinking_blocks=[
                {"type": "thinking", "thinking": "".join(reasoning_text), "signature": ""}
            ]
            if any(reasoning_text)
            else [],
        )


# 返回事件使用的 UTC 时间
def _now() -> str:
    return datetime.now(UTC).isoformat()
