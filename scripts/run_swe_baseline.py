"""SWE-bench Verified 单题 B0 基线运行器。

只做一件事：把一个冻结清单里的题目交给 LoopSmith 的 AgentLoop 跑一次，
保存候选补丁与全部原始产物，供官方 harness 独立验收。

默认只准备并展示计划，必须显式传 --execute 才会真正调用模型。
本脚本不负责批量实验，也不参与官方评分。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from loopsmith.core.context import ExecutionContext  # noqa: E402
from loopsmith.core.events.bus import EventBus  # noqa: E402
from loopsmith.core.events.writer import EventWriter  # noqa: E402
from loopsmith.core.llm.factory import create_provider  # noqa: E402
from loopsmith.core.loop import AgentLoop  # noqa: E402
from loopsmith.core.tools.base import BaseTool, ToolResult  # noqa: E402
from loopsmith.core.tools.builtin.bash import BashTool  # noqa: E402
from loopsmith.core.tools.registry import ToolRegistry  # noqa: E402
from loopsmith.evaluation.data import verify, write_json  # noqa: E402
from loopsmith.evaluation.feedback import RunTestsTool, test_feedback  # noqa: E402
from loopsmith.evaluation.isolation import ISOLATE_GIT_SCRIPT  # noqa: E402
from loopsmith.evaluation.reproducer import IssueReproducerTool, candidate_fingerprint  # noqa: E402
from loopsmith.evaluation.token_budget import TokenBudget  # noqa: E402

# B0 固定预算：8 次模型调用、单次 2048 输出 token、单次 60 秒、整题 240 秒、不自动重跑
B0_MAX_STEPS = 8
B0_MAX_OUTPUT_TOKENS = 2048
B0_CALL_TIMEOUT_S = 60
B0_RUN_TIMEOUT_S = 240
B0_RUN_RETRIES = 0
B0_STREAM_RETRIES = 1
B0_TOOL_TIMEOUT_S = 90
B0_MAX_OUTPUT_BYTES = 32 * 1024
B0_TOOL_MEMORY = "2g"
B0_TOOL_CPUS = "2"
# b5 P0：docker 默认 cpu.shares=1024，pylint 的 _query_cpu() 因此读到 1 核，
# 把 4 个 @pytest.mark.needs_two_cores 的 P2P 测试改成 skip，SWE-bench 记 failure。
# 这里给 2048 让容器内视角稳定是 2 核（cpus 仍是硬上限 2，不改变算力）。
B0_TOOL_CPU_SHARES = "2048"

# 题目工作区在容器里的位置：直接用官方镜像的 /testbed。
# 镜像里 editable 安装的包（easy-install.pth）正好指向这里，模型跑 pytest
# 检验的就是自己改的那份源码；.git 与预编译扩展也都在，不需要额外挂载。
WORKDIR = "/testbed"


class ContainerFileTool(BaseTool):
    """按 kind 实现 read / write / edit / list，路径一律相对 /testbed。"""

    _MAX_READ_BYTES = 512 * 1024
    _MAX_WRITE_BYTES = 1 * 1024 * 1024
    # edit_file 要整文件读入再写回，超过这个大小就拒绝，避免读截断导致写回丢内容
    _MAX_EDIT_BYTES = 800 * 1024

    def __init__(self, container: TaskContainer, kind: str, guard: Any = None) -> None:
        self.container = container
        self.kind = kind
        self.guard = guard
        if kind == "read":
            self.name = "read_file"
            self.description = (
                "Read the text content of a file. "
                f"Path is relative to {WORKDIR}. Files larger than 512 KB are truncated."
            )
        elif kind == "write":
            self.name = "write_file"
            self.description = (
                "Write text content to a file, creating it (and any parent directories) if it "
                f"does not exist, or overwriting it if it does. Path is relative to {WORKDIR}. "
                "Content size is limited to 1 MB. For edits to an existing file prefer "
                "edit_file, which cannot silently clobber unrelated lines."
            )
        elif kind == "edit":
            self.name = "edit_file"
            self.description = (
                "Replace one exact snippet in an existing file and return the resulting diff. "
                "`old_string` must appear exactly once in the file, otherwise the edit is "
                "rejected — include enough surrounding lines to make it unique. "
                "Prefer this over write_file/sed for editing existing code: it reports the "
                "diff, so you can review what you actually changed."
            )
        else:
            self.name = "list_dir"
            self.description = f"List directory contents. Path is relative to {WORKDIR}."
        self.input_schema = self._schema(kind)

    @staticmethod
    def _schema(kind: str) -> dict[str, object]:
        if kind == "write":
            return {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"Relative path under {WORKDIR}."},
                    "content": {"type": "string", "description": "Text content to write."},
                },
                "required": ["path", "content"],
            }
        if kind == "edit":
            return {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"Relative path under {WORKDIR}."},
                    "old_string": {
                        "type": "string",
                        "description": "Exact existing text to replace; must occur exactly once.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Text to put in its place (may be empty to delete).",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            }
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": f"Relative path under {WORKDIR}."}
            },
            "required": ["path"],
        }

    @staticmethod
    def _relpath(params: dict[str, object]) -> str:
        raw = str(params.get("path", "."))
        if Path(raw).is_absolute():
            raise PermissionError(f"Use a path relative to {WORKDIR}, e.g. requests/models.py")
        if ".." in Path(raw).parts:
            raise PermissionError(f"path traversal not allowed: {raw}")
        return raw

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            rel = self._relpath(params)
        except PermissionError as exc:
            return ToolResult(str(exc), True, "permission_denied")
        quoted = shlex.quote(rel)

        if self.kind == "write":
            content = str(params.get("content", ""))
            encoded = content.encode("utf-8")
            if len(encoded) > self._MAX_WRITE_BYTES:
                return ToolResult(
                    f"content too large: {len(encoded)} bytes (limit 1 MB)",
                    True,
                    "runtime_error",
                )
            rc, out = await self.container.exec_bytes(
                f"mkdir -p -- $(dirname -- {quoted}) && cat > {quoted}", stdin=encoded
            )
            if rc != 0:
                return ToolResult(out or "write failed", True, "runtime_error")
            if self.guard is not None:
                self.guard.note_edit(rel)
            return ToolResult(f"wrote {len(encoded)} bytes to {rel}")

        if self.kind == "read":
            limit = self._MAX_READ_BYTES
            rc, out = await self.container.exec_bytes(
                f"if [ -f {quoted} ]; then head -c {limit + 1} -- {quoted}; "
                f"elif [ -d {quoted} ]; then echo '__IS_DIR__'; else echo '__MISSING__'; fi",
                output_limit=limit + 1,
            )
            if out.startswith("__MISSING__"):
                return ToolResult(f"file not found: {rel}", True, "runtime_error")
            if out.startswith("__IS_DIR__"):
                return ToolResult(f"{rel} is a directory, use list_dir", True, "runtime_error")
            raw = out.encode("utf-8", errors="replace")
            truncated = len(raw) > limit
            text = raw[:limit].decode("utf-8", errors="replace") if truncated else out
            if truncated:
                text += "\n[truncated]"
            return ToolResult(text)

        if self.kind == "edit":
            return await self._edit(rel, quoted, params)

        rc, out = await self.container.exec(f"ls -la -- {quoted}")
        if rc != 0:
            return ToolResult(out or f"cannot list {rel}", True, "runtime_error")
        return ToolResult(out)

    # 精确替换一处片段：old_string 必须唯一命中，返回 unified diff 供模型复核。
    # 读写都走 base64，避免 docker exec 的文本解码改动字节（换行/非 ASCII）。
    async def _edit(self, rel: str, quoted: str, params: dict[str, object]) -> ToolResult:
        old = params.get("old_string")
        new = params.get("new_string")
        if not isinstance(old, str) or not old:
            return ToolResult(
                "old_string must be a non-empty string; use write_file to create a file",
                True,
                "schema_error",
            )
        if not isinstance(new, str):
            return ToolResult("new_string must be a string", True, "schema_error")
        if old == new:
            return ToolResult(
                "old_string and new_string are identical, nothing to change", True, "schema_error"
            )

        limit = self._MAX_EDIT_BYTES
        rc, probe = await self.container.exec_bytes(
            f"if [ ! -e {quoted} ]; then echo __MISSING__; "
            f"elif [ -d {quoted} ]; then echo __IS_DIR__; "
            f"elif [ $(wc -c < {quoted}) -gt {limit} ]; then echo __TOO_BIG__; "
            f"else base64 -w0 -- {quoted}; fi",
            output_limit=limit * 2 + 256,
        )
        if probe.startswith("__MISSING__"):
            return ToolResult(
                f"file not found: {rel}; use write_file to create it", True, "runtime_error"
            )
        if probe.startswith("__IS_DIR__"):
            return ToolResult(f"{rel} is a directory", True, "runtime_error")
        if probe.startswith("__TOO_BIG__"):
            return ToolResult(
                f"{rel} is larger than {limit} bytes; edit_file will not rewrite it whole. "
                "Use a targeted bash command instead.",
                True,
                "runtime_error",
            )
        try:
            text = base64.b64decode(probe.strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ToolResult(
                f"{rel} could not be read as UTF-8 text; edit it with bash instead",
                True,
                "runtime_error",
            )

        occurrences = text.count(old)
        if occurrences == 0:
            return ToolResult(
                f"old_string not found in {rel}. Read the file and copy the exact text, "
                "including indentation and surrounding lines.",
                True,
                "edit_conflict",
            )
        if occurrences > 1:
            return ToolResult(
                f"old_string appears {occurrences} times in {rel}; it must match exactly once. "
                "Include more surrounding lines to disambiguate.",
                True,
                "edit_conflict",
            )

        updated = text.replace(old, new, 1)
        payload = base64.b64encode(updated.encode("utf-8"))
        rc, out = await self.container.exec_bytes(f"base64 -d > {quoted}", stdin=payload)
        if rc != 0:
            return ToolResult(out or "write failed", True, "runtime_error")

        diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        if len(diff) > 6000:
            diff = diff[:6000] + "\n[diff truncated]"
        if self.guard is not None:
            self.guard.note_edit(rel)
        return ToolResult(f"edited {rel} (1 replacement)\n{diff}")


class TaskContainer:
    """一题一个常驻容器，**不使用 bind mount**。

    镜像里的 /testbed 是官方预构建的：完整 .git、编译好的扩展模块、symlink
    全都在。之前的做法是把宿主机工作区 bind mount 到 /testbed，等于用一份
    纯源码把这些资产全盖住 —— astropy 因此 import 直接失败，所有题也都
    丢掉 git（`git log` 报 not a git repository）。这里改成让容器保持镜像
    原样，宿主机不再参与文件系统。
    """

    def __init__(self, image: str, name: str) -> None:
        self.image = image
        self.name = name
        self.started = False

    def start(self) -> None:
        proc = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.name,
                "--pull=never",
                "--network=none",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                f"--memory={B0_TOOL_MEMORY}",
                f"--cpus={B0_TOOL_CPUS}",
                f"--cpu-shares={B0_TOOL_CPU_SHARES}",
                "--pids-limit=512",
                "--tmpfs",
                "/tmp:rw,size=512m",
                "--workdir",
                WORKDIR,
                "--entrypoint",
                "sleep",
                self.image,
                "infinity",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise SystemExit(f"容器启动失败: {proc.stderr.strip()[:400]}")
        self.started = True

    # 容器内执行命令；timeout 在容器内生效，避免超时后进程残留在容器里
    async def exec_bytes(
        self,
        command: str,
        timeout_s: int = B0_TOOL_TIMEOUT_S,
        stdin: bytes | None = None,
        output_limit: int = B0_MAX_OUTPUT_BYTES,
    ) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            "--workdir",
            WORKDIR,
            self.name,
            "timeout",
            "-s",
            "KILL",
            str(int(timeout_s)),
            "bash",
            "-lc",
            "set -o pipefail; " + command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=timeout_s + 30
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return 124, f"Command timed out after {timeout_s}s"
        text = output[:output_limit].decode("utf-8", errors="replace")
        if len(output) > output_limit:
            text += f"\n[output truncated: {len(output)} bytes total; returned {output_limit}]"
        return proc.returncode or 0, text

    async def exec(self, command: str, timeout_s: int = B0_TOOL_TIMEOUT_S) -> tuple[int, str]:
        return await self.exec_bytes(command, timeout_s=timeout_s)

    def copy_out(self, source: str, destination: Path) -> None:
        proc = subprocess.run(
            ["docker", "cp", f"{self.name}:{source}", str(destination)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise SystemExit(f"docker cp 失败: {proc.stderr.strip()[:400]}")

    def stop(self) -> None:
        if not self.started:
            return
        subprocess.run(
            ["docker", "rm", "-f", self.name], capture_output=True, text=True, timeout=120
        )
        self.started = False


class ContainerBashTool(BaseTool):
    """在本题常驻容器里执行命令：镜像原样、无网络、无宿主密钥。"""

    name = "bash"
    description = (
        "Run a non-interactive shell command inside the task container. "
        f"The repository is at {WORKDIR} and that is the working directory; "
        "its Python environment already has this project installed in editable mode, "
        "so `python -m pytest` runs against your edits. Network access is disabled."
    )
    input_schema = BashTool.input_schema

    def __init__(
        self, container: TaskContainer, guard: Any = None, restrict_search: bool = False
    ) -> None:
        self.container = container
        self.guard = guard
        self.restrict_search = restrict_search

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult("command must be a non-empty string", True, "schema_error")
        if self.restrict_search:
            try:
                words = shlex.split(command)
            except ValueError:
                words = []
            forbidden_path = any(
                word == "/" or word.startswith(("~", "/root", "/home", "$HOME", "${HOME}"))
                or ".cache/pip" in word or ".cache/uv" in word
                for word in words
            )
            forbidden_lookup = re.search(
                r"\bpip[0-9]*\s+(?:cache|download|install)\b|\bgit\s+(?:log|show|rev-list)\b",
                command,
            )
            if forbidden_path or forbidden_lookup:
                return ToolResult(
                    "Out-of-scope lookup blocked. Inspect the checked-out /testbed source "
                    "and installed runtime behavior. Do not search filesystem roots, home "
                    "directories, package caches, alternate versions, or Git history for "
                    "an implementation. Build the fix from the issue and current source.",
                    True, "scope_error",
                )
        rc, text = await self.container.exec(command, timeout_s=B0_TOOL_TIMEOUT_S)
        if self.guard is not None:
            self.guard.note_command(command, rc, text)
        if rc == 0:
            return ToolResult(text)
        if rc == 124:
            return ToolResult(f"{text}\n[exit code 124]", True, "timeout")
        # 非零退出码是**信息**而不是故障：跑一遍失败的 pytest 本来就返回 1。
        # 用 runtime_error 会被 invoke_tool 当成瞬时错误重试 3 次，等于每条
        # 失败命令都多等一轮，所以这里给一个不可重试的类型。
        return ToolResult(f"{text}\n[exit code {rc}]", True, "command_failed")


# b5 P2：收尾守卫。
# b4 的失败题里有一类共同形态：最后一次编辑之后直接 end_turn，没有跑测试；
# django-11149 那次"验证"实际输出是 `No module named pytest`，模型仍当成验证过了。
# 这里把「最后编辑之后必须有一次真实测试执行」变成硬约束，只在还有步数时提醒，
# 避免把"撞上限"变成"多一次空转"。
_TEST_RUN_RE = re.compile(
    r"(?:^|[|;&(]\s*|\bsudo\s+)(?:[\w./-]*/)?(?:pytest|py\.test)\b"
    r"|\bpython[0-9.]*\s+-m\s+(?:pytest|unittest)\b"
    r"|\b(?:\./)?tests/runtests\.py\b"
    r"|\b(?:\./)?bin/test\b"
    r"|\btox\b|\bnox\b"
)
_EDIT_CMD_RE = re.compile(
    r"sed\s+-[a-zA-Z]*i|perl\s+-[a-zA-Z]*i|patch\s+-p|git\s+apply|\btee\b|truncate\s"
    r"|(?<![0-9&>])>(?![&>])\s*(?!/dev/null)\S"
)
_TEST_FILE_RE = re.compile(
    r"(^|/)(tests?|testing)/|/(test_[^/]*\.py|[^/]*_test\.py|conftest\.py)$"
)


class FinishGuard:
    """收尾守卫：编辑后未验证、改测试文件时注入提醒，让模型继续一轮。

    只统计"看起来像测试入口"的命令（pytest / runtests.py / bin/test / tox），
    不用退出码判定成败——测试失败本身是有效信号，说明模型确实看到了结果。
    """

    def __init__(
        self, test_command: str | None = None, max_nudges: int = 2, on_nudge: Any = None
    ) -> None:
        self.test_command = test_command
        self.max_nudges = max_nudges
        self.on_nudge = on_nudge
        self.seq = 0
        self.last_edit_seq = 0
        self.last_verify_seq = 0
        self.last_verify_rc: int | None = None
        self.verify_runs = 0
        self.nudges = 0
        self.test_files_touched: list[str] = []
        self.warned_test_files = False
        self.nudge_reasons: list[str] = []
        self.last_feedback: dict[str, Any] | None = None

    def note_edit(self, rel: str) -> None:
        self.seq += 1
        self.last_edit_seq = self.seq
        if _TEST_FILE_RE.search(rel) and rel not in self.test_files_touched:
            self.test_files_touched.append(rel)

    def note_command(self, command: str, rc: int, output: str) -> None:
        self.seq += 1
        feedback = test_feedback(rc, output)
        if _TEST_RUN_RE.search(command) and feedback["valid_test_run"]:
            self.note_test(feedback)
        if _EDIT_CMD_RE.search(command):
            self.last_edit_seq = self.seq

    # 只有真实收集并执行的测试才能覆盖当前编辑版本，错误和空测试不能算验证
    def note_test(self, feedback: dict[str, Any]) -> None:
        self.last_feedback = feedback
        if feedback["valid_test_run"]:
            self.seq += 1
            self.last_verify_seq = self.seq
            self.last_verify_rc = 0 if feedback["status"] == "passed" else 1
            self.verify_runs += 1

    async def before_finish(self, context: ExecutionContext) -> str | None:
        del context  # 步数余量由 AgentLoop 判断，这里只看工具侧状态
        if self.nudges >= self.max_nudges:
            return None
        problems: list[str] = []
        if self.last_edit_seq > self.last_verify_seq:
            if self.last_verify_seq == 0:
                problems.append("You have not run the project's tests at all.")
            else:
                problems.append(
                    "You edited files after your last test run, so that result does not "
                    "cover your current change."
                )
            if self.test_command:
                problems.append(
                    "Use run_tests now. It preserves the real exit status and test summary."
                )
            else:
                problems.append("Run the project's tests now and read the output.")
        elif self.last_feedback is not None and not self.last_feedback["valid_test_run"]:
            problems.append(
                "The latest test attempt did not execute a valid passing test run "
                f"(status={self.last_feedback['status']}). Diagnose it and use run_tests again; "
                "an earlier passing run is not evidence that this attempt passed."
            )
        elif self.last_verify_rc not in (0, None):
            problems.append(
                f"Your last test run exited with code {self.last_verify_rc}, which means "
                "tests are still failing. Read the failures and fix them."
            )
        if self.test_files_touched and not self.warned_test_files:
            self.warned_test_files = True
            listed = ", ".join(self.test_files_touched[:5])
            problems.append(
                f"Your patch modifies test files ({listed}). The grader restores the "
                "original test files before running them, so a green result that depends "
                "on your test edits will not count. Fix the source code instead."
            )
        if not problems:
            return None
        self.nudges += 1
        self.nudge_reasons.append(problems[0])
        message = (
            "Not done yet — before you finish:\n- "
            + "\n- ".join(problems)
            + "\n\nAddress this with tools, then finish. Reply only when the tests "
            "have run against your final edits."
        )
        if self.on_nudge is not None:
            self.on_nudge(f"[guard] nudge {self.nudges}/{self.max_nudges}: {problems[0]}")
        return message


class ConvergenceGuard(FinishGuard):
    # 为定位和验证分别保留预算，所有提醒仍计入原有步数上限
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.phase_reminders: list[str] = []
        self.localization_checked = False

    # 中途无编辑与最后四步分别提醒一次，防止整题停留在探索阶段
    async def before_step(self, context: ExecutionContext) -> str | None:
        if context.max_steps < 8:
            return None
        remaining = context.max_steps - context.step + 1
        message = None
        if remaining <= 4 and "verification" not in self.phase_reminders:
            self.phase_reminders.append("verification")
            message = (
                f"Only {remaining} calls remain, including this one. Reserve them for "
                "an issue-derived assertion, run_tests, and reviewing the final diff. "
                "Stop broad searches. Check EVERY public API and behavior named in the "
                "issue, not just the linked source location. If verification fails, use "
                "the concrete failure to make one minimal correction. Do not claim a fix "
                "when only old regression tests pass."
            )
        elif (
            context.step >= context.max_steps // 2
            and not self.localization_checked
        ):
            self.localization_checked = True
            fingerprint = getattr(self, "fingerprint", None)
            issue = getattr(self, "issue", None)
            edited = (
                await fingerprint() != issue.initial_fingerprint
                if fingerprint and issue else self.last_edit_seq > 0
            )
            if edited:
                return None
            self.phase_reminders.append("localization")
            message = (
                "Half the call budget has been used without a recorded edit. Stop broad "
                "exploration: summarize your hypothesis, reproduce the reported behavior "
                "with an assertion, and make the smallest justified source change. "
                "Leave at least four calls for verification. If evidence is insufficient, "
                "report the blocker instead of guessing."
            )
        if message and self.on_nudge:
            self.on_nudge(f"[policy] {self.phase_reminders[-1]}: {remaining} calls remaining")
        return message


class VerifiedRepairGuard(ConvergenceGuard):
    # 区分问题复现、回归结果和模型对预期差异的解释，最终成功仍由独立验收决定
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.issue: IssueReproducerTool | None = None
        self.fingerprint: Any = None
        self.verification_status = "not_checked"
        self.reviewed_boundary = False

    # 所有证据必须对应当前代码，不能用旧通过状态掩盖后续编辑
    async def evidence(self) -> tuple[bool, str]:
        if self.issue is None or self.fingerprint is None:
            return False, "Issue reproduction is unavailable."
        current = await self.fingerprint()
        if self.issue.baseline_returncode is None:
            return False, "Record a failing baseline with check_issue before changing source."
        if self.issue.verified_fingerprint != current:
            return False, "Run check_issue with no script against your current candidate."
        feedback = self.last_feedback or {}
        if not feedback.get("valid_test_run") or feedback.get("candidate_fingerprint") != current:
            return False, "Run run_tests on the current candidate and inspect the actual results."
        if feedback["status"] == "passed":
            self.verification_status = "issue_and_regression_passed"
            return True, ""
        if feedback["status"] == "failed" and self.issue.explanation:
            self.verification_status = "issue_passed_regression_failures_explained_by_model"
            return True, ""
        return False, (
            "Some old tests failed. Inspect each failure: do not revert behavior explicitly "
            "requested by the issue just to satisfy outdated assertions. If AND ONLY IF each "
            "mismatch is justified by the issue, rerun check_issue with "
            "explain_test_failures documenting the mismatches. Fix unrelated regressions. "
            "Leave existing tests unchanged. Explanations are claims, not passing tests."
        )

    # 在原调用预算内提示缺失证据，耗尽提醒后明确终止为未验证而不伪称成功
    async def before_finish(self, context: ExecutionContext) -> str | None:
        ready, reason = await self.evidence()
        if ready:
            if not self.reviewed_boundary and context.max_steps - context.step >= 3:
                self.reviewed_boundary = True
                return (
                    "One final source review within the remaining budget: did you replace "
                    "the faulty operation at its abstraction boundary, or only special-case "
                    "the sample input? If the issue requests an existing general helper, "
                    "use that helper consistently for the supported input domain unless "
                    "the documented API requires a narrower change. Inspect all issue-named "
                    "interfaces. Do not change unrelated behavior or existing tests. If "
                    "you edit again, rerun check_issue and run_tests; otherwise finish."
                )
            return None
        if self.nudges >= self.max_nudges:
            context.mark_failed("verification_incomplete")
            return None
        self.nudges += 1
        self.nudge_reasons.append(reason)
        if self.on_nudge:
            self.on_nudge(f"[guard] evidence {self.nudges}/{self.max_nudges}: {reason[:180]}")
        return reason

    # 即使步数耗尽也保存最终证据状态，不把工具成功或模型结束当作问题修复
    async def after_run(self, context: ExecutionContext) -> None:
        try:
            ready, _ = await self.evidence()
            if not ready:
                self.verification_status = "incomplete"
        except Exception:
            ready = False
            self.verification_status = "validation_error"
        if context.status == "success" and not ready:
            context.mark_failed("verification_incomplete")


_TEST_OUTPUT_START = ">>>>> Start Test Output"
_TEST_OUTPUT_END = ">>>>> End Test Output"


def extract_test_command(instance_id: str) -> str | None:
    """从数据集 eval_script 里取本题的测试命令（只取命令，不含 test_patch）。

    21 个镜像里有 5 个没有 pytest（django 用 tests/runtests.py、sympy 用 bin/test），
    b4 中模型一律猜 `python -m pytest`，django-11149 直接报 No module named pytest
    却仍当成验证过了。这里把官方命令给出去，不泄露测试内容。
    """
    path = ROOT / ".eval-data/swe-bench-verified.json"
    for row in json.loads(path.read_text(encoding="utf-8")):
        if row.get("instance_id") != instance_id:
            continue
        commands: list[str] = []
        capture = False
        for line in str(row.get("eval_script") or "").splitlines():
            if _TEST_OUTPUT_START in line:
                capture = True
                continue
            if _TEST_OUTPUT_END in line:
                capture = False
                continue
            if capture and line.strip():
                commands.append(line.strip())
        return " && ".join(commands) if commands else None
    return None


class ObservedProvider:
    """包装真实 provider：记录调用次数/用量，按固定间隔发送，错误立即上抛不重跑。"""

    def __init__(
        self,
        provider: Any,
        emit: Any,
        secrets: list[str],
        *,
        request_interval_s: float = 0.0,
        max_steps: int = B0_MAX_STEPS,
        max_output_tokens: int = 4096,
        usage_input_includes_cache: bool = False,
    ) -> None:
        self.provider = provider
        self.emit = emit
        self.secrets = [value for value in secrets if value]
        self.max_steps = max_steps
        self.max_output_tokens = max_output_tokens
        self.usage_input_includes_cache = usage_input_includes_cache
        self.token_budget = TokenBudget(ROOT / ".eval-data/optimization-20261020/token-ledger.json")
        self.request_interval_s = request_interval_s
        self.calls = 0
        self.error: str | None = None
        self.error_kind: str | None = None
        self.usages: list[dict[str, int]] = []

    def _redact(self, message: str) -> str:
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        return message

    async def chat(self, **kwargs: Any) -> Any:
        if self.request_interval_s > 0:
            await asyncio.sleep(self.request_interval_s)
        reservation = self.token_budget.reserve(kwargs, self.max_output_tokens)
        self.calls += 1
        self.emit(f"[model] request {self.calls}/{self.max_steps}; budget reserved={reservation}")
        started = time.monotonic()
        try:
            # 单次调用硬上限，不依赖各 provider 内部默认超时
            response = await asyncio.wait_for(
                self.provider.chat(**kwargs), timeout=B0_CALL_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001 - 需要保留原始错误类型用于判定
            self.error_kind = _classify_error(exc)
            self.error = self._redact(f"{type(exc).__name__}: {exc}")[:2000]
            self.emit(
                f"[model] failed ({self.error_kind}) after "
                f"{time.monotonic() - started:.1f}s: {self.error[:300]}"
            )
            raise
        elapsed = time.monotonic() - started
        usage = getattr(response, "usage", None)
        if usage is not None:
            cache_read = int(usage.cache_read_input_tokens)
            cache_creation = int(usage.cache_creation_input_tokens)
            prompt_tokens = int(usage.input_tokens)
            if not self.usage_input_includes_cache:
                prompt_tokens += cache_read + cache_creation
            self.token_budget.settle(reservation, prompt_tokens + int(usage.output_tokens))
            self.usages.append(
                {
                    "input_tokens": int(usage.input_tokens),
                    "output_tokens": int(usage.output_tokens),
                    "prompt_tokens_including_cache": prompt_tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_creation,
                }
            )
        self.emit(
            f"[model] response stop={response.stop_reason} "
            f"tools={len(response.tool_calls)} {elapsed:.1f}s"
        )
        return response


# 区分限流、超时和其他错误，便于在结果里如实标注停止原因
def _classify_error(exc: BaseException) -> str:
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if status == 429 or "RateLimit" in name:
        return "rate_limited_429"
    if "Timeout" in name or isinstance(exc, TimeoutError):
        return "timeout"
    if status is not None:
        return f"http_{status}"
    return name


# 按冻结清单校验题目，拒绝清单外实例
def load_instance(prepared: Path, instance_id: str) -> dict[str, Any]:
    manifest = verify(ROOT / ".eval-data", prepared)
    for task in manifest["swe_bench"]:
        if task["instance_id"] == instance_id:
            return task
    raise SystemExit(f"{instance_id} is not in the frozen selection; refusing to run")


# 返回本题镜像名，数据集行里带着官方镜像标签
def instance_image(instance_id: str) -> str:
    rows = json.loads((ROOT / ".eval-data/swe-bench-verified.json").read_text(encoding="utf-8"))
    for row in rows:
        if row["instance_id"] == instance_id:
            return str(row["image"])
    raise SystemExit(f"{instance_id} not found in local SWE-bench snapshot")


# 模型常在仓库里留下脚手架：__pycache__、.pytest_cache、编译产物。
# 它们不该进官方补丁：二进制文件没法用 git apply 还原会直接判应用失败，
# 缓存目录纯属噪声。这里在出 diff 前把它们从索引里摘掉并如实记录。
_NOISE_DIRS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "node_modules"}
)


def _is_noise_path(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part in _NOISE_DIRS for part in parts)


# 把容器里的 /testbed 对齐到 base_commit，并记录一份「起始快照」。
#
# 为什么要快照：官方镜像的工作树并非绝对干净 —— requests-1142 就带着 setup.py
# 留下的未跟踪 build/（65 个文件、871KB）。如果拿 base_commit 当补丁基线，
# 这些镜像自带的产物会被算成模型的改动，补丁直接膨胀且大概率 apply 失败。
# 用起始快照（git write-tree）作基线，补丁就精确等于"模型改了什么"，
# 而 harness 施加补丁的对象也正是这份起始状态。
async def align_to_base(
    container: TaskContainer, base_commit: str, isolate_history: bool = False
) -> dict[str, object]:
    rc, head = await container.exec(f"git -C {WORKDIR} rev-parse HEAD")
    head = head.strip()
    if rc != 0:
        raise SystemExit(f"容器内 git 不可用: {head[:300]}")

    rc, kind = await container.exec(f"git -C {WORKDIR} cat-file -t {base_commit}")
    if rc != 0 or kind.strip() != "commit":
        raise SystemExit(f"镜像里没有 base_commit {base_commit} 的对象，无法作为补丁基线")

    rc, changed = await container.exec(f"git -C {WORKDIR} diff --name-only {base_commit} HEAD")
    checked_out = False
    if rc == 0 and changed.strip():
        rc, out = await container.exec(f"git -C {WORKDIR} checkout -f {base_commit}")
        if rc != 0:
            raise SystemExit(f"checkout 到 base_commit 失败: {out[:300]}")
        checked_out = True

    rc, history_count = await container.exec(
        f"git rev-list --all --not {base_commit} | wc -l"
    )
    if rc != 0:
        raise SystemExit("无法审计镜像 Git 可达范围，停止评测")
    if isolate_history:
        # 只清理无挂载的一次性任务容器内的 Git 元数据，工作树和宿主仓库不受影响
        rc, original_tree = await container.exec("git add -A . && git write-tree")
        if rc != 0:
            raise SystemExit("隔离前无法记录源码树")
        rc, out = await container.exec_bytes(
            "python -", stdin=ISOLATE_GIT_SCRIPT.encode("utf-8"), timeout_s=180,
        )
        if rc != 0:
            raise SystemExit(f"隔离评测 Git 历史失败: {out[:300]}")

    rc, out = await container.exec(f"cd {WORKDIR} && git add -A . && git write-tree")
    start_tree = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
    if not start_tree:
        raise SystemExit(f"无法记录起始快照: {out[:300]}")
    if isolate_history and start_tree != original_tree.strip():
        raise SystemExit("隔离 Git 历史改变了源码树，停止评测；未发送模型请求")

    rc, status = await container.exec(f"git -C {WORKDIR} status --porcelain")
    return {
        "image_head": head,
        "checked_out_to_base": checked_out,
        "start_tree": start_tree,
        "dirty_entries_at_start": len([x for x in status.splitlines() if x.strip()]),
        "nonancestor_commits_before_isolation": int(history_count.strip()),
        "history_policy": "source_snapshot_only" if isolate_history else "image_history",
    }


# 在容器内生成「相对起始快照」的标准 git diff。
# 全程用镜像自带的 git：符号链接、可执行位、换行都由 Linux 侧决定，
# 不再受 Windows 文件系统影响（旧方案里 symlink 会被压成 0 字节文件）。
async def export_patch(
    container: TaskContainer, start_tree: str, base_commit: str
) -> tuple[str, list[str]]:
    dropped: list[str] = []
    # 不能放 /tmp：容器给 /tmp 挂了 tmpfs，docker cp 看不到 tmpfs 里的文件
    # （实测报 "Could not find the file ... in container"）。/root 在镜像层，可读。
    patch_in_container = "/root/loopsmith-candidate.patch"

    # add -A 尊重 .gitignore，编译产物不会进来；仍然显式剔除噪声与二进制
    rc, out = await container.exec(f"cd {WORKDIR} && git add -A .", timeout_s=300)
    if rc != 0:
        raise SystemExit(f"git add 失败: {out[:400]}")

    rc, numstat = await container.exec(
        f"cd {WORKDIR} && git diff --cached --numstat {start_tree}", timeout_s=300
    )
    if rc != 0:
        raise SystemExit(f"git diff --numstat 失败: {numstat[:400]}")
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, _deleted, rel = parts
        if added == "-" or _is_noise_path(rel):
            dropped.append(rel)
    if dropped:
        quoted = " ".join(shlex.quote(rel) for rel in dropped)
        rc, out = await container.exec(
            f"cd {WORKDIR} && git reset -q -- {quoted}", timeout_s=120
        )
        if rc != 0:
            raise SystemExit(f"剔除噪声文件失败: {out[:400]}")

    # --no-textconv：避免仓库配置的 textconv 驱动在容器里缺失导致整条 diff 失败
    rc, out = await container.exec(
        f"cd {WORKDIR} && git diff --cached {start_tree} "
        f"--no-color --no-textconv --binary > {patch_in_container}",
        timeout_s=300,
    )
    if rc != 0:
        raise SystemExit(f"git diff 失败: {out[:400]}")
    rc, out = await container.exec(f"wc -c < {patch_in_container}")
    if rc != 0:
        raise SystemExit(f"补丁文件未生成: {out[:300]}")

    # 走 docker cp 而不是 stdout：二进制补丁必须字节级原样取出
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False, suffix=".patch") as tmp:
        local = Path(tmp.name).resolve()
    try:
        container.copy_out(patch_in_container, local)
        patch = local.read_bytes().decode("utf-8", errors="replace")
    finally:
        local.unlink(missing_ok=True)
    return patch, dropped


# 记录当前运行时代码版本与工作区改动摘要，不只记 HEAD
def code_version() -> dict[str, Any]:
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    unstaged = subprocess.run(["git", "-C", str(ROOT), "diff"], capture_output=True).stdout
    staged = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--cached"], capture_output=True
    ).stdout
    status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    import hashlib

    return {
        "head": head,
        "dirty": bool(status.strip()),
        "worktree_diff_sha256": hashlib.sha256(unstaged + staged).hexdigest(),
        "changed_files": len([line for line in status.splitlines() if line.strip()]),
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in (ROOT / "src", ROOT / "scripts")
            for path in sorted(folder.rglob("*.py"))
        },
    }


# 把原始问题描述与统一要求拼成任务输入，不添加任何修复位置提示
def build_prompt(
    problem_statement: str, test_command: str | None = None, repair_policy: str = "feedback"
) -> str:
    if test_command:
        verify_lines = (
            "- This repository does not necessarily use `python -m pytest`. The grader "
            "will run exactly this command from the repository root:\n"
            f"    {test_command}\n"
            "  Running it is the cheapest way to check your change. Read the output: a "
            "traceback or an import error is not a passing test run.\n"
        )
    else:
        verify_lines = (
            "- The container already has this project installed in editable mode, so you "
            "can verify your change by running its tests with bash, for example "
            "`python -m pytest <file>::<test>`. Prefer checking over guessing.\n"
        )
    return (
        f"You are working inside a Linux container. The repository is at "
        f"{WORKDIR} and that is the current working directory; use relative paths "
        "such as `requests/models.py`.\n\n"
        "Problem report:\n"
        f"{problem_statement.strip()}\n\n"
        + (
            "Repair workflow:\n"
            "1. Inspect the issue and relevant source. List every named public API and behavior.\n"
            "When replacing a faulty generic operation with an existing safe helper, "
            "apply it at the abstraction boundary across the supported input domain; "
            "do not retain the original operation in ad hoc branches just to preserve "
            "old sample expectations. Respect the documented API and the issue's scope.\n"
            "2. BEFORE editing source, call check_issue with a small Python assertion script "
            "that demonstrates the reported failure. Use synthetic data and temporary "
            "directories. A failing baseline is expected; import/syntax errors are not proof.\n"
            "3. Make a minimal source fix. Call check_issue with NO script to rerun the exact "
            "same assertions, then run_tests. Each result is bound to current source content.\n"
            "4. Inspect regressions. Existing tests may assert behavior explicitly changed by "
            "the issue. Do not blindly restore that behavior just to make old tests green. "
            "Only if the issue justifies each mismatch, record the explanation via "
            "check_issue(explain_test_failures=...). Never excuse unrelated regressions or "
            "modify existing tests. Report any remaining failures truthfully.\n"
            "5. Use at most half the calls for exploration and reserve four for verification. "
            "Do not search Git history, caches, alternate versions, or filesystem roots for answers.\n\n"
            if repair_policy == "verified" else
            (
            "Repair workflow:\n"
            "1. Briefly list ALL public APIs, edge cases, and promised behaviors in the issue. "
            "A linked file is a starting point, not the entire scope. Inspect each relevant path.\n"
            "Do not search Git history, package caches, archives, other installed copies, "
            "or filesystem roots for an implementation. Work from the checked-out source.\n"
            "2. Create a small local assertion-based reproducer from the issue, using synthetic "
            "data when the example downloads data. Run it before editing and confirm that it "
            "fails for the reported reason, not imports or syntax.\n"
            "3. Make a minimal source change. Run the SAME assertions after editing and "
            "run_tests. Cover each named API, not only the first example.\n"
            "4. Read the actual outputs and final git diff. Stop when both reproduction and "
            "regressions pass; otherwise state exactly what remains unverified. Use at most "
            "half the calls on exploration and reserve four calls for validation.\n\n"
            if repair_policy == "converge" else ""
            )
        )
        + "Requirements:\n"
        "- Inspect the relevant source code before editing.\n"
        "- Fix the reported problem in this repository.\n"
        "- Preserve public interfaces and behavior unrelated to the bug.\n"
        "- Do not delete, skip, or modify existing tests to make them pass: the grader "
        "restores the original test files before running them, so green tests that "
        "depend on your test edits do not count.\n"
        f"- Do not access anything outside {WORKDIR}. There is no network access.\n"
        "- Use `git diff` to review exactly what you changed "
        "before you finish.\n"
        "- Tools: read_file, list_dir, edit_file, write_file, run_tests, and bash (runs in the "
        f"container at {WORKDIR}). Prefer edit_file when changing an existing file: it "
        "replaces one exact snippet, rejects ambiguous anchors, and returns the diff. "
        "Keep the diff minimal — change only what the bug requires.\n"
        + verify_lines
        + "- Use run_tests for the fixed regression check; it preserves real test status. "
        "An empty run, import error, or pipeline exit status 0 is not a passing test.\n"
        + "- When the fix is complete, reply with a short summary and call no more tools.\n"
        "- Stop calling tools as soon as the fix holds. Do not keep re-verifying or "
        "exploring after the change works."
    )


# 检查镜像是否已在本地，避免运行器隐式触发大体积下载
def image_present(image: str) -> bool:
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True)
    return result.returncode == 0


# 容器名要唯一：同一题可能被并行重跑，pid + 时间戳避免撞名
def _container_name(instance_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", instance_id).lower()
    return f"loopsmith-{safe[:40]}-{os.getpid()}-{int(time.time())}"


async def run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env", override=False)
    prepared = (ROOT / ".eval-data/frozen-v1").resolve()
    task = load_instance(prepared, args.instance_id)
    base_commit = str(task["base_commit"])
    repo = str(task["repo"])
    image = instance_image(args.instance_id)

    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".eval-data"):
        raise SystemExit("输出目录必须位于 .eval-data 下")
    if args.execute and output.exists():
        raise SystemExit(f"输出目录已存在，拒绝覆盖：{output}")

    from loopsmith.core.config import get_config

    config = get_config()
    key_name = (
        "HKU_API_KEY"
        if config.llm.provider == "hku"
        else "DEEPSEEK_API_KEY"
        if "deepseek" in (config.llm.base_url or "")
        or config.llm.default_model.startswith("deepseek")
        else "ANTHROPIC_API_KEY"
    )
    api_key = (os.environ.get(key_name) or "").strip()
    if not api_key:
        raise SystemExit(f"{key_name} 为空，未发送任何请求")

    test_command = extract_test_command(args.instance_id)
    prompt = build_prompt(str(task["problem_statement"]), test_command, args.repair_policy)
    # 预算常量默认取 B0；受控实验可用 CLI 覆盖，覆盖值同样写入 config.json
    max_steps = args.max_steps if args.max_steps is not None else B0_MAX_STEPS
    run_timeout_s = args.run_timeout_s if args.run_timeout_s is not None else B0_RUN_TIMEOUT_S
    max_output_tokens = (
        args.max_output_tokens if args.max_output_tokens is not None else B0_MAX_OUTPUT_TOKENS
    )
    # 默认用本题官方镜像做命令执行环境：里面装着该仓库真实依赖与 pytest，
    # 模型才能自己跑测试验证（此前用 python:3.12-slim 时无法自检）
    bash_image = args.bash_image or image
    plan = {
        "kind": "B0_single_task_swe_baseline",
        "instance_id": args.instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "provider": config.llm.provider,
        "model": config.llm.default_model,
        "base_url": config.llm.base_url,
        "thinking": f"enabled-{args.thinking_effort}" if args.thinking_enabled else "disabled",
        "repair_policy": args.repair_policy,
        "history_policy": "source_snapshot_only" if args.repair_policy != "feedback" else "image_history",
        "external_search_filter": args.repair_policy != "feedback",
        "credential_env": key_name,
        "grading_image": image,
        "grading_image_present": image_present(image),
        "bash_image": bash_image,
        "bash_image_present": image_present(bash_image),
        "budget": {
            "max_steps": max_steps,
            "max_output_tokens_per_call": max_output_tokens,
            "call_timeout_s": B0_CALL_TIMEOUT_S,
            "run_timeout_s": run_timeout_s,
            "run_retries": B0_RUN_RETRIES,
            "stream_retries": B0_STREAM_RETRIES,
            "sdk_max_retries": 0,
            "request_interval_s": args.request_interval_s,
            "tool_timeout_s": B0_TOOL_TIMEOUT_S,
            "tool_memory": B0_TOOL_MEMORY,
            "tool_cpus": B0_TOOL_CPUS,
            "tool_cpu_shares": B0_TOOL_CPU_SHARES,
        },
        "tools": ["read_file", "list_dir", "edit_file", "write_file", "run_tests", "bash"] + (["check_issue"] if args.repair_policy == "verified" else []),
        "test_command": test_command,
        "test_command_injected": test_command is not None,
        "finish_guard": {
            "enabled": True,
            "max_nudges": 2,
            "rule": ("问题基线失败、同一断言在当前候选通过，回归绑定内容指纹；预期差异只记录模型解释"
                     if args.repair_policy == "verified" else
                     "最后一次编辑之后必须有一次测试命令执行；改测试文件给一次告警"),
        },
        "workspace": (
            f"{WORKDIR} (container native, no bind mount: image .git, compiled "
            "extensions and symlinks preserved, network disabled)"
        ),
        "sandbox_mode": "A-container-native",
        "bash_note": (
            "每题一个常驻容器，保留预编译扩展；converge 策略将 Git 历史隔离为源码快照。"
            "模型可以直接 python -m pytest 自检，也可以用 git diff 复核自己的改动"
        ),
        "disabled": ["rag", "memory", "compaction", "subagent", "permission_prompts"],
        "prompt": prompt,
        "code_version": code_version(),
        "python": sys.version.split()[0],
    }

    if not args.execute:
        output.mkdir(parents=True, exist_ok=True)
        write_json(output / "config.json", plan)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print("\n[plan] 仅准备，未调用模型。加 --execute 才会真实运行。")
        return 0

    if not plan["bash_image_present"]:
        raise SystemExit(f"本地缺少命令执行镜像 {bash_image}；未发送任何模型请求。")

    output.mkdir(parents=True, exist_ok=False)
    (output / "patches").mkdir()
    (output / "grading").mkdir()

    write_json(output / "config.json", {**plan, "env_ready": True})
    log_handle = (output / "live.log").open("w", encoding="utf-8")

    def emit(message: str) -> None:
        print(message, flush=True)
        log_handle.write(message + "\n")
        log_handle.flush()

    emit(f"[task] {args.instance_id} @ {base_commit[:12]}")
    emit(
        f"[model] provider={config.llm.provider} model={config.llm.default_model} "
        f"base_url={config.llm.base_url}"
    )
    emit(
        f"[budget] {max_steps} steps, {max_output_tokens} output tokens/call, "
        f"{B0_CALL_TIMEOUT_S}s/call, {run_timeout_s}s total, no run retry"
    )
    emit(f"[image] bash={bash_image} grading={image}")
    emit(f"[sandbox] {WORKDIR} 容器原生（不挂载），网络关闭")

    bus = EventBus()
    tool_counts: dict[str, int] = {}
    usage_events: list[dict[str, int]] = []

    async def observe(event: BaseModel) -> None:
        item = event.model_dump()
        kind = item.get("type")
        if kind == "step.started":
            emit(f"[step {item['step']}] planning")
        elif kind == "tool.call_started":
            tool = str(item["tool_name"])
            tool_counts[tool] = tool_counts.get(tool, 0) + 1
            params = item.get("params") or {}
            preview = params.get("path") or params.get("command", "")
            emit(f"[tool] {tool}: {str(preview)[:200]}")
        elif kind in ("tool.call_finished", "tool.call_failed"):
            status = "ok" if kind == "tool.call_finished" else "error"
            emit(f"[tool] {kind.split('.')[1]} {item['tool_name']}: {status}")
        elif kind == "llm.usage":
            usage_events.append(
                {
                    "input_tokens": int(item["input_tokens"]),
                    "output_tokens": int(item["output_tokens"]),
                }
            )

    bus.subscribe(observe)
    registry = ToolRegistry()
    container = TaskContainer(bash_image, _container_name(args.instance_id))
    guard_type = (VerifiedRepairGuard if args.repair_policy == "verified" else
                  ConvergenceGuard if args.repair_policy == "converge" else FinishGuard)
    guard = guard_type(test_command, on_nudge=emit)
    patch = ""
    patch_error: str | None = None
    dropped: list[str] = []
    elapsed = 0.0
    timed_out = False
    base_info: dict[str, object] = {}
    started = 0.0
    provider: ObservedProvider | None = None
    context = ExecutionContext(output.name, prompt, max_steps)
    setup_error: str | None = None

    try:
        container.start()
        base_info = await align_to_base(
            container, base_commit, isolate_history=args.repair_policy != "feedback"
        )
        emit(
            f"[sandbox] image_head={str(base_info['image_head'])[:12]} "
            f"checkout_to_base={base_info['checked_out_to_base']} "
            f"dirty_at_start={base_info['dirty_entries_at_start']}"
        )

        registry = ToolRegistry()
        for kind in ("read", "write", "edit", "list"):
            registry.register(ContainerFileTool(container, kind, guard))
        registry.register(ContainerBashTool(
            container, guard, restrict_search=args.repair_policy != "feedback"
        ))
        if test_command:
            registry.register(RunTestsTool(
                container, test_command, guard,
                log_dir=output / "test-logs" if args.repair_policy == "verified" else None,
            ))
        if isinstance(guard, VerifiedRepairGuard):
            guard.fingerprint = partial(candidate_fingerprint, container)
            initial_fingerprint = await guard.fingerprint()
            guard.issue = IssueReproducerTool(
                container, initial_fingerprint, guard.fingerprint, output / "issue-evidence"
            )
            registry.register(guard.issue)

        base_provider = create_provider(
            config.llm,
            max_output_tokens=max_output_tokens,
            max_stream_retries=B0_STREAM_RETRIES,
            timeout_s=B0_CALL_TIMEOUT_S,
            sdk_max_retries=0,
            deepseek_thinking=args.thinking_enabled,
            deepseek_thinking_effort=args.thinking_effort,
        )
        provider = ObservedProvider(
            base_provider,
            emit,
            [api_key],
            request_interval_s=args.request_interval_s,
            max_steps=max_steps,
            max_output_tokens=max_output_tokens,
            usage_input_includes_cache=config.llm.provider == "hku",
        )
        context = ExecutionContext(output.name, prompt, max_steps)
        emit("[prompt] " + prompt.replace("\n", "\n         "))

        started = time.monotonic()
        async with EventWriter(output / "events.jsonl") as writer:
            writer.subscribe(bus)
            try:
                async with asyncio.timeout(run_timeout_s):
                    await AgentLoop(provider, registry, bus, finish_guard=guard).run(context)
            except TimeoutError:
                timed_out = True
                context.mark_failed("run_timeout")
                emit(f"[stop] 整题超过 {run_timeout_s}s，已终止")
            except Exception as exc:  # noqa: BLE001 - 保留原始失败状态
                if context.status == "running":
                    context.mark_failed(_classify_error(exc))
                emit(f"[stop] 模型阶段异常终止：{type(exc).__name__}")
    except (Exception, SystemExit) as exc:
        context.mark_failed("sandbox_initialization_failed" if provider is None else "runner_error")
        setup_error = f"{type(exc).__name__}: {exc}".replace(api_key, "[REDACTED]")[:2000]
        emit(f"[stop] {context.reason}: {setup_error[:300]}")
    finally:
        elapsed = round(time.monotonic() - started, 3) if started else 0.0
        log_handle.flush()
        # 补丁必须在停止容器之前导出；导出失败也要留下 result.json，
        # 否则这一轮在批量统计里会变成"无记录"
        if container.started and base_info.get("start_tree"):
            try:
                patch, dropped = await export_patch(
                    container, str(base_info["start_tree"]), base_commit
                )
                patch_path = output / "patches" / f"{args.instance_id}.patch"
                patch_path.write_text(patch, encoding="utf-8", newline="\n")
                emit(f"[patch] {len(patch)} bytes -> patches/{patch_path.name}")
                if dropped:
                    emit(f"[patch] 剔除 {len(dropped)} 个噪声文件: {', '.join(dropped[:8])}")
            except BaseException as exc:  # noqa: BLE001 - 连 SystemExit 一起兜住
                patch_error = f"{type(exc).__name__}: {exc}"
                emit(f"[patch] 导出失败：{patch_error[:300]}")
        container.stop()

    usages = (provider.usages or usage_events) if provider is not None else usage_events
    api_calls = provider.calls if provider is not None else 0
    result: dict[str, Any] = {
        "instance_id": args.instance_id,
        "base_commit": base_commit,
        "provider": config.llm.provider,
        "model": config.llm.default_model,
        "agent_status": context.status,
        "reason": context.reason,
        "steps": context.step,
        "api_calls": api_calls,
        "api_error": provider.error if provider is not None else None,
        "api_error_kind": provider.error_kind if provider is not None else None,
        "setup_error": setup_error,
        "timed_out": timed_out,
        "usage_complete": len(usages) == api_calls,
        "usage_records": len(usages),
        "input_tokens": sum(u["input_tokens"] for u in usages),
        "output_tokens": sum(u["output_tokens"] for u in usages),
        "prompt_tokens_including_cache": sum(u.get("prompt_tokens_including_cache", 0) for u in usages),
        "token_ledger": ".eval-data/optimization-20261020/token-ledger.json",
        "tool_counts": tool_counts,
        "tool_calls_total": sum(tool_counts.values()),
        "agent_elapsed_s": elapsed,
        "sandbox_mode": "A-container-native",
        "sandbox_base": base_info,
        "patch_bytes": len(patch),
        "patch_error": patch_error,
        "patch_written": bool(patch.strip()),
        "patch_dropped": dropped,
        "finish_guard": {
            "nudges": guard.nudges,
            "nudge_reasons": guard.nudge_reasons,
            "verify_runs": guard.verify_runs,
            "last_verify_rc": guard.last_verify_rc,
            "edited_after_last_verify": guard.last_edit_seq > guard.last_verify_seq,
            "test_files_touched": guard.test_files_touched,
            "max_nudges": guard.max_nudges,
            "phase_reminders": getattr(guard, "phase_reminders", []),
            "boundary_review_requested": getattr(guard, "reviewed_boundary", False),
        },
        "answer": (context.result or "")[:4000],
        "verification_status": getattr(guard, "verification_status", "legacy_command_evidence"),
        "issue_evidence": ({
            "script_sha256": guard.issue.script_sha256,
            "baseline_returncode": guard.issue.baseline_returncode,
            "last_returncode": guard.issue.last_returncode,
            "verified_fingerprint": guard.issue.verified_fingerprint,
            "explanation_is_model_claim": True,
            "test_failure_explanation": guard.issue.explanation,
            "attempts": guard.issue.attempts,
        } if isinstance(guard, VerifiedRepairGuard) and guard.issue else None),
    }
    write_json(output / "result.json", result)
    emit(
        f"[done] agent={context.status} reason={context.reason} "
        f"calls={api_calls} tools={result['tool_calls_total']} "
        f"patch={result['patch_bytes']}B"
    )
    log_handle.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one frozen SWE-bench instance through LoopSmith (B0 baseline)"
    )
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--thinking-enabled", action="store_true", help="同款 DeepSeek 开低强度推理；默认关闭")
    parser.add_argument("--thinking-effort", choices=("low", "high"), default="low")
    parser.add_argument("--repair-policy", choices=("feedback", "converge", "verified"), default="feedback")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="真正调用模型；缺省只准备并展示计划",
    )
    parser.add_argument(
        "--request-interval-s",
        type=float,
        default=0.0,
        help="两次模型请求之间的固定间隔秒数，会写入 config.json",
    )
    parser.add_argument(
        "--bash-image",
        default=None,
        help="覆盖 Agent 命令执行镜像；缺省用本题官方镜像（自带依赖与 pytest）",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=f"覆盖 B0 的步数上限（默认 {B0_MAX_STEPS}），用于单变量对照实验",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help=f"覆盖单次输出 token 上限（默认 {B0_MAX_OUTPUT_TOKENS}）",
    )
    parser.add_argument(
        "--run-timeout-s",
        type=int,
        default=None,
        help=f"覆盖整题时限秒数（默认 {B0_RUN_TIMEOUT_S}）",
    )
    args = parser.parse_args()
    if args.request_interval_s < 0:
        parser.error("--request-interval-s 不能为负数")
    if args.max_steps is not None and args.max_steps <= 0:
        parser.error("--max-steps 必须为正整数")
    if args.max_output_tokens is not None and args.max_output_tokens <= 0:
        parser.error("--max-output-tokens 必须为正整数")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
