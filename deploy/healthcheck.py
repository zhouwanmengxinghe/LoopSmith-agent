from __future__ import annotations

import json
import socket
import sys


# 通过真实 Ping 响应检查 Core 就绪，不创建会话或调用模型
def check_health(host: str = "127.0.0.1", port: int = 7437) -> None:
    request = {
        "jsonrpc": "2.0", "id": "docker-health", "method": "core.ping",
        "params": {"client": "docker-health"},
    }
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.settimeout(2)
        connection.sendall((json.dumps(request) + "\n").encode())
        with connection.makefile("rb") as stream:
            response = json.loads(stream.readline(65537))
    if (
        response.get("jsonrpc") != "2.0"
        or response.get("id") != "docker-health"
        or not isinstance(response.get("result"), dict)
        or not isinstance(response["result"].get("server_version"), str)
        or not isinstance(response["result"].get("uptime_ms"), int)
    ):
        raise ValueError("Core returned an invalid Ping response")


if __name__ == "__main__":
    try:
        check_health()
    except (OSError, ValueError, TypeError, AttributeError):
        print("LoopSmith Core is not ready", file=sys.stderr)
        raise SystemExit(1) from None
