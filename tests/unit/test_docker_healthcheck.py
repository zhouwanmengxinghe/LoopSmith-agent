from __future__ import annotations

import io
import json
import socket
from typing import Any

import pytest
from deploy.healthcheck import check_health


class _Connection:
    # 保存模拟响应与客户端发出的请求
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.sent = b""

    # 模拟 socket 上下文管理
    def __enter__(self) -> _Connection:
        return self

    # 模拟连接关闭
    def __exit__(self, *args: object) -> None:
        pass

    # 校验健康检查为读取设置了有限超时
    def settimeout(self, timeout: int) -> None:
        assert timeout == 2

    # 捕获请求，确认不发送任何任务或密钥
    def sendall(self, payload: bytes) -> None:
        self.sent = payload

    # 返回模拟的换行 JSON 响应
    def makefile(self, mode: str) -> io.BytesIO:
        return io.BytesIO((json.dumps(self.response) + "\n").encode())


# 功能：健康检查只调用 Ping，不创建任务或触发模型请求
# 设计：捕获完整请求并校验协议、响应与超时，覆盖无需真实网络的就绪路径
def test_healthcheck_only_sends_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection({
        "jsonrpc": "2.0", "id": "docker-health",
        "result": {"server_version": "0.0.1", "uptime_ms": 1},
    })
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: connection)
    check_health()
    request = json.loads(connection.sent)
    assert request == {
        "jsonrpc": "2.0", "id": "docker-health", "method": "core.ping",
        "params": {"client": "docker-health"},
    }


# 功能：错误响应、无关服务或不完整响应不能被判为 Core 就绪
# 设计：构造五类伪健康响应，防止只要 TCP 端口打开就放行启动
@pytest.mark.parametrize("response", [
    {"jsonrpc": "2.0", "id": "docker-health", "error": {"code": -32603}},
    {"jsonrpc": "2.0", "id": "other", "result": {"server_version": "x", "uptime_ms": 1}},
    {"jsonrpc": "2.0", "id": "docker-health", "result": {}},
    {"id": "docker-health", "result": {"server_version": "x", "uptime_ms": 1}},
    {"jsonrpc": "2.0", "id": "docker-health", "result": "ok"},
])
def test_healthcheck_rejects_invalid_response(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any],
) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: _Connection(response))
    with pytest.raises(ValueError, match="invalid Ping"):
        check_health()
