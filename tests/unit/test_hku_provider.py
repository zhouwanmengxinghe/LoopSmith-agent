from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from loopsmith.core.config import LoopSmithConfig, _apply_env, _apply_toml
from loopsmith.core.context import ExecutionContext
from loopsmith.core.events.bus import EventBus
from loopsmith.core.llm.factory import create_provider
from loopsmith.core.llm.hku import HkuProvider, convert_messages
from loopsmith.core.loop import AgentLoop
from loopsmith.core.tools.base import BaseTool, ToolResult
from loopsmith.core.tools.registry import ToolRegistry


# 生成可控 SSE，使用真实 HTTPX 编解码但完全不访问网络
def stream_response(chunks: list[dict]) -> httpx.Response:
    payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    return httpx.Response(
        200, text=payload + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
    )


# 功能：验证 HKU 认证、工具 schema、空 delta、分片参数与缓存统计正确
# 设计：MockTransport 检查实际 HTTP 请求，覆盖曾发生的 null 工具片段而不消耗额度
async def test_hku_stream_tools_and_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HKU_API_KEY", "test-secret")

    # 检查出站格式并返回分片工具调用
    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.hku.hk/foundry/student/models/chat/completions"
        assert request.headers["api-key"] == "test-secret"
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == "DeepSeek-V4-Flash"
        assert body["tools"][0]["function"]["parameters"]["type"] == "object"
        return stream_response(
            [
                {"choices": [{"delta": {"content": "Looking"}}]},
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": None}]}}
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"name": "read_file", "arguments": None},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"name": None, "arguments": '{"path":'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 120,
                        "completion_tokens": 15,
                        "prompt_tokens_details": {"cached_tokens": 80},
                    },
                },
            ]
        )

    provider = HkuProvider(
        "DeepSeek-V4-Flash", context_window=1000, transport=httpx.MockTransport(handle)
    )
    result = await provider.chat(
        [],
        [{"name": "read_file", "description": "read", "input_schema": {"type": "object"}}],
        EventBus(),
        "r",
    )
    assert result.stop_reason == "tool_use"
    assert result.tool_calls[0].input == {"path": "a.py"}
    assert result.usage is not None and result.usage.context_pct == 0.12
    assert result.usage.cache_read_input_tokens == 80


# 功能：验证现有消息历史可以转换为完整的工具请求和结果序列
# 设计：同一步两个结果应分别成为 tool 消息，thinking 不可泄漏到普通正文
def test_message_conversion() -> None:
    result = convert_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "a"}},
                    {"type": "tool_use", "id": "c2", "name": "read", "input": {"path": "b"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "A"},
                    {"type": "tool_result", "tool_use_id": "c2", "content": "B", "is_error": True},
                ],
            },
        ],
        "system",
    )
    assert [item["role"] for item in result] == ["system", "assistant", "tool", "tool"]
    assert result[1]["tool_calls"][1]["id"] == "c2"
    assert result[1]["reasoning_content"] == "private"
    assert result[1]["content"] is None


# 功能：验证截断、阻断、畸形参数及错误响应不会执行工具或假装成功
# 设计：覆盖 finish_reason 和 HTTP 边界，错误正文包含假密钥以检查不被输出
@pytest.mark.parametrize("mode", ["truncated", "bad_json", "invalid_type", "blocked", "http"])
async def test_invalid_streams_fail_closed(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HKU_API_KEY", "test-secret")

    # 根据用例构造异常响应
    def handle(request: httpx.Request) -> httpx.Response:
        if mode == "http":
            return httpx.Response(429, text="test-secret must not be logged")
        delta = {
            "tool_calls": [{"index": 0, "id": "c", "function": {"name": "read", "arguments": "{"}}]
        }
        if mode == "invalid_type":
            delta = {"content": 7}  # type: ignore[dict-item]
        finish = {"blocked": "content_filter", "bad_json": "tool_calls"}.get(mode)
        return stream_response([{"choices": [{"delta": delta, "finish_reason": finish}]}])

    with pytest.raises((ValueError, RuntimeError)) as error:
        await HkuProvider("m", transport=httpx.MockTransport(handle)).chat([], [], EventBus(), "r")
    assert "test-secret" not in str(error.value)


# 功能：验证达到输出上限时丢弃不完整工具调用并保留 max_tokens 状态
# 设计：截断 JSON 不应尝试解析，更不能误作 end_turn
async def test_length_does_not_execute_partial_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HKU_API_KEY", "test")
    transport = httpx.MockTransport(
        lambda request: stream_response(
            [
                {
                    "choices": [
                        {
                            "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{"}}]},
                            "finish_reason": "length",
                        }
                    ]
                },
            ]
        )
    )
    result = await HkuProvider("m", transport=transport).chat([], [], EventBus(), "r")
    assert result.stop_reason == "max_tokens" and result.tool_calls == []
    assert result.usage is None


# 功能：验证认证不允许重定向到非 HKU 主机且缺少密钥时直接失败
# 设计：先拒绝错误目的地，再以 MockTransport 排除真实请求
async def test_host_and_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        HkuProvider("m", base_url="https://api.hku.hk.evil.example/models")
    monkeypatch.delenv("HKU_API_KEY", raising=False)
    with pytest.raises(ValueError, match="HKU_API_KEY"):
        await HkuProvider("m").chat([], [], EventBus(), "r")


# 功能：验证 HKU 配置在 TOML、环境变量和工厂中贯通
# 设计：使用纯配置对象，确认不改默认供应商也不读取真实密钥
def test_hku_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = LoopSmithConfig()
    _apply_toml(config, {"llm": {"provider": "hku", "max_output_tokens": 1024}})
    monkeypatch.setenv("LOOPSMITH_LLM_DEFAULT_MODEL", "DeepSeek-V4-Flash")
    monkeypatch.setenv("LOOPSMITH_LLM_PROVIDER", "hku")
    _apply_env(config)
    assert isinstance(create_provider(config.llm), HkuProvider)
    assert config.llm.max_output_tokens == 1024
    with pytest.raises(SystemExit):
        _apply_toml(config, {"llm": {"timeout_s": -1}})


class EchoTool(BaseTool):
    name = "echo"
    description = "echo"
    input_schema = {"type": "object"}

    # 返回确定工具结果，用来检查下一轮发给模型的消息
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("tool-ok")


# 功能：验证现有 AgentLoop 能完成 HKU 工具请求、结果回填和最终回答
# 设计：两轮真实格式 HTTP 模拟覆盖适配器与循环的交界，不单独伪造 LlmResponse
async def test_existing_loop_hku_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HKU_API_KEY", "test")
    requests = []

    # 第一轮调用工具，第二轮检查工具结果并结束
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            delta = {
                "tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "echo", "arguments": "{}"}}
                ]
            }
            finish = "tool_calls"
        else:
            assert body["messages"][-1] == {
                "role": "tool",
                "tool_call_id": "c1",
                "content": "tool-ok",
            }
            delta, finish = {"content": "finished"}, "stop"
        return stream_response([{"choices": [{"delta": delta, "finish_reason": finish}]}])

    registry = ToolRegistry()
    registry.register(EchoTool())
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集现有事件，验证工具执行仍经过原有事件机制
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(collect)
    provider = HkuProvider("m", transport=httpx.MockTransport(handle))
    context = ExecutionContext("r", "test", 3)
    await AgentLoop(provider, registry, bus).run(context)
    assert context.status == "success" and context.result == "finished"
    assert context.step == 2
    assert any(getattr(event, "type", "") == "tool.call_finished" for event in events)
