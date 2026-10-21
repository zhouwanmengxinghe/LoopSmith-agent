from __future__ import annotations

import asyncio
import locale
import os
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from loopsmith.core.tools.base import BaseTool, ToolResult

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 60
_WINDOWS_CREATE_NO_WINDOW = 0x08000000


class BashParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


# 查找可用的 POSIX shell：跳过 System32 下的 WSL bash（其路径语义与 Windows 不一致）
def _find_bash() -> str | None:
    found = shutil.which("bash")
    if found is not None and "system32" not in found.lower():
        return found
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    for root in roots:
        if not root:
            continue
        for relative in (
            "Git/bin/bash.exe",
            "Git/usr/bin/bash.exe",
            "Programs/Git/bin/bash.exe",
        ):
            candidate = Path(root) / relative
            if candidate.is_file():
                return str(candidate)
    return found


# 选择可用的 shell：Windows 优先 Git Bash，其次 PowerShell，最后 cmd.exe
def _shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        bash = _find_bash()
        if bash is not None:
            return [bash, "-c", command]
        pwsh = shutil.which("pwsh") or shutil.which("powershell")
        if pwsh is not None:
            return [pwsh, "-NoProfile", "-NonInteractive", "-Command", command]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    return [os.environ.get("SHELL", "/bin/sh"), "-c", command]


# 解码子进程输出：优先 UTF-8，失败回退系统本地编码（中文 Windows 为 cp936）
def _decode_output(data: bytes) -> str:
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# 超时后结束整个进程树，避免 Windows 上只杀掉 shell 而留下孤儿子进程
async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    proc.kill()


class BashTool(BaseTool):
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined). "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Prefer short, focused commands. Output is truncated at 64 KB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        command = p.command
        timeout = p.timeout

        spawn_kwargs: dict[str, object] = {}
        if os.name == "nt":
            # 守护进程无控制台，避免每条命令弹出窗口
            spawn_kwargs["creationflags"] = _WINDOWS_CREATE_NO_WINDOW

        try:
            proc = await asyncio.create_subprocess_exec(
                *_shell_argv(command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **spawn_kwargs,  # type: ignore[arg-type]
            )
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                await _kill_tree(proc)
                await proc.communicate()
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        output = _decode_output(stdout_bytes)
        truncated = len(stdout_bytes) > _MAX_OUTPUT_BYTES
        if truncated:
            output = output[:_MAX_OUTPUT_BYTES] + "\n[truncated]"

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")
