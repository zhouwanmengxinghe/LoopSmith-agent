from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator

import pytest


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port  # socket released; daemon can bind to this port


# Windows 上后台拉起 daemon 时不创建控制台窗口
def _daemon_spawn_kwargs() -> dict[str, int]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


# 结束 daemon；Windows 需用 taskkill /T 一并清理子进程，terminate 只杀父进程
def _stop_daemon(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture
async def running_daemon(free_port: int) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    env = os.environ.copy()
    env["LOOPSMITH_PORT"] = str(free_port)
    env["LOOPSMITH_LOG_FILE"] = ""
    env["LOOPSMITH_LOG_LEVEL"] = "WARNING"

    proc = subprocess.Popen(
        [sys.executable, "-m", "loopsmith.core"],
        env=env,
        **_daemon_spawn_kwargs(),
    )

    # Windows 冷启动 Python + 导入 textual/httpx 较慢，给足 10 秒
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        _stop_daemon(proc)
        pytest.fail("Daemon did not start within 10 seconds")

    yield proc

    _stop_daemon(proc)
