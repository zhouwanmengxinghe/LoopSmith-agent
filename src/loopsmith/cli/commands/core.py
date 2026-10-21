from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from loopsmith.core.config import LoopSmithConfig

_PID_FILE = Path.home() / ".loopsmith" / "loopsmith-core.pid"

# Windows 进程探活所需的常量（GetExitCodeProcess 返回 259 表示仍在运行）
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: LoopSmithConfig) -> None:
    _r, w = await asyncio.open_connection(config.host, config.port)
    w.close()
    await w.wait_closed()


# Windows 上 os.kill(pid, 0) 会抛 WinError 87，改用 OpenProcess 查询退出码
def _windows_pid_alive(pid: int) -> bool:
    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 拿不到句柄通常说明进程已退出；权限不足时保守认为仍在运行
        return bool(ctypes.get_last_error() == _ERROR_ACCESS_DENIED)
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return bool(code.value == _STILL_ACTIVE)
    finally:
        kernel32.CloseHandle(handle)


# 跨平台判断 pid 对应的进程是否存活
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


# 读取 PID 文件并确认进程存活，进程已消失则删除文件并返回 None
def _running_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        _PID_FILE.unlink(missing_ok=True)
        return None
    return pid


# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: LoopSmithConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: LoopSmithConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"already running  ({config.host}:{config.port})")
        return
    except (ConnectionRefusedError, OSError):
        pass

    # POSIX 用 start_new_session 脱离终端；Windows 必须用 DETACHED_PROCESS，
    # 否则父进程退出后子进程会随控制台一起被回收
    spawn_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        spawn_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        ) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        spawn_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-m", "loopsmith.core"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **spawn_kwargs,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    # 启动后短暂确认进程没有立即崩掉，避免打印 started 却在后台静默退出
    time.sleep(0.4)
    if proc.poll() is not None:
        _PID_FILE.unlink(missing_ok=True)
        print(
            f"error: core exited immediately (exit code {proc.returncode}). "
            "Run `python -m loopsmith.core` in the foreground to see the traceback."
        )
        return
    print(f"started  pid={proc.pid}  ({config.host}:{config.port})")


# 停止 daemon：Windows 使用 taskkill 结束进程树，POSIX 发送 SIGTERM
def cmd_core_stop(config: LoopSmithConfig) -> None:
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    if os.name == "nt":
        # Windows 没有 SIGTERM 语义，/T 一并结束 MCP 等子进程，避免孤儿进程
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        os.kill(pid, signal.SIGTERM)
    _PID_FILE.unlink(missing_ok=True)
    print(f"stopped  pid={pid}")
