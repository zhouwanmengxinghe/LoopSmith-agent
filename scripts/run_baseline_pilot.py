from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from loopsmith.core.context import ExecutionContext  # noqa: E402
from loopsmith.core.events.bus import EventBus  # noqa: E402
from loopsmith.core.events.writer import EventWriter  # noqa: E402
from loopsmith.core.llm.hku import HkuProvider  # noqa: E402
from loopsmith.core.llm.types import LlmResponse  # noqa: E402
from loopsmith.core.loop import AgentLoop  # noqa: E402
from loopsmith.core.tools.base import BaseTool, ToolResult  # noqa: E402
from loopsmith.core.tools.builtin import ListDirTool, ReadFileTool, WriteFileTool  # noqa: E402
from loopsmith.core.tools.builtin.bash import BashTool  # noqa: E402
from loopsmith.core.tools.registry import ToolRegistry  # noqa: E402
from loopsmith.evaluation.data import sha256, write_json  # noqa: E402
from loopsmith.evaluation.grading import grade_quix  # noqa: E402


class ScopedFileTool(BaseTool):
    # 复用现有工具 schema，仅将读写边界限制为题目工作区
    def __init__(self, inner: BaseTool, workspace: Path) -> None:
        self.inner, self.workspace = inner, workspace.resolve()
        self.name, self.description = inner.name, inner.description
        self.input_schema = inner.input_schema

    # 拒绝绝对路径、目录越界和非目标文件写入
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        raw = str(params.get("path", "."))
        path = Path(raw)
        resolved = (self.workspace / path).resolve()
        if path.is_absolute() or not resolved.is_relative_to(self.workspace):
            return ToolResult("Path must stay inside the task workspace.", True, "permission_denied")
        if self.name == "write_file" and resolved != self.workspace / "python_programs/gcd.py":
            return ToolResult("Only python_programs/gcd.py is editable.", True, "permission_denied")
        if self.name == "list_dir" and any(p.is_symlink() for p in resolved.rglob("*")):
            return ToolResult("Symlinks are not allowed.", True, "permission_denied")
        return await self.inner.invoke(params)


class ContainerBashTool(BaseTool):
    name = "bash"
    description = "Run a non-interactive shell command in the task's isolated Linux container. Python is available; internet is disabled."
    input_schema = BashTool.input_schema

    # 保存仅挂载题目代码的工作目录
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    # 使用已有本地镜像执行命令，不传入宿主密钥且不访问外网
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        command = params.get("command")
        if not isinstance(command, str):
            return ToolResult("command must be a string", True, "schema_error")
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm", "--pull=never", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=256m",
            "--cpus=1", "--pids-limit=64", "--tmpfs", "/tmp:rw,nosuid,size=16m",
            "--mount", f"type=bind,source={self.workspace.as_posix()},target=/workspace",
            "--workdir", "/workspace", "python:3.12-slim", "sh", "-c", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return ToolResult("Container command timeout", True, "timeout")
        return ToolResult(output[:16000].decode("utf-8", errors="replace"),
                          proc.returncode != 0, "runtime_error" if proc.returncode else None)


class ObservedProvider:
    # 包装现有 HKU 适配器，只记录调用次数、返回状态与安全错误信息
    def __init__(self, provider: HkuProvider, emit: Any) -> None:
        self.provider, self.emit = provider, emit
        self.calls = 0
        self.error: str | None = None

    # 执行单次真实调用，不在网络或模型错误后自动重跑
    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.calls += 1
        self.emit(f"[HKU] request {self.calls}/8")
        try:
            result = await self.provider.chat(**kwargs)
        except Exception as exc:
            self.error = str(exc).replace(os.environ["HKU_API_KEY"], "[REDACTED]")
            self.emit(f"[HKU] stopped: {self.error}")
            raise
        self.emit(f"[HKU] response: {result.stop_reason}, tools={len(result.tool_calls)}")
        return result


# 执行一题有明确预算的真实模型试跑，并以独立测试给出验收结果
async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    key = os.environ.get("HKU_API_KEY", "").strip()
    if not key:
        raise SystemExit("HKU_API_KEY is empty; no request sent")
    if not os.environ.get("HTTPS_PROXY"):
        raise SystemExit("HTTPS_PROXY must be set; no request sent")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".eval-data"):
        raise SystemExit("Pilot output must be inside project .eval-data")
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / "workspace"
    source = ROOT / ".eval-data/frozen-v1/agent/quixbugs/gcd"
    shutil.copytree(source / "python_programs", workspace / "python_programs",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    candidate = workspace / "python_programs/gcd.py"
    original = candidate.read_text(encoding="utf-8")
    (output / "before.py").write_text(original, encoding="utf-8")
    model = os.environ.get("LOOPSMITH_LLM_DEFAULT_MODEL", "DeepSeek-V4-Flash")
    config = {"kind": "B0_single_task_connectivity_pilot", "instance_id": "quixbugs-gcd",
              "model": model, "max_steps": 8, "max_output_tokens": 2048,
              "whole_run_timeout_s": 240, "automatic_run_retries": 0, "rag": False,
              "tools": ["read_file", "write_file", "list_dir", "bash"],
              "shell": "local python:3.12-slim, network disabled, only task workspace mounted",
              "source_sha256": sha256(candidate)}
    write_json(output / "config.json", config)
    log = (output / "live.log").open("w", encoding="utf-8")

    # 同时向终端和实时文件写入进展，不记录认证头或 reasoning_content
    def emit(message: str) -> None:
        print(message, flush=True)
        log.write(message + "\n")
        log.flush()

    usage: list[dict[str, Any]] = []
    bus = EventBus()

    # 展示步骤、工具名、执行状态和 token 用量
    async def observe(event: BaseModel) -> None:
        item = event.model_dump()
        kind = item.get("type")
        if kind == "step.started":
            emit(f"[step {item['step']}] planning")
        elif kind == "tool.call_started":
            params = item["params"]
            preview = params.get("path") or params.get("command", "")
            emit(f"[tool] {item['tool_name']}: {str(preview)[:300]}")
        elif kind in ("tool.call_finished", "tool.call_failed"):
            emit(f"[tool] {item['tool_name']}: {kind.rsplit('_', 1)[-1]}")
        elif kind == "llm.usage":
            usage.append(item)
            emit(f"[usage] input={item['input_tokens']}, output={item['output_tokens']}")

    bus.subscribe(observe)
    registry = ToolRegistry()
    for inner in (ReadFileTool(), WriteFileTool(), ListDirTool()):
        registry.register(ScopedFileTool(inner, workspace))
    registry.register(ContainerBashTool(workspace))
    provider = ObservedProvider(HkuProvider(model, max_output_tokens=2048, timeout_s=60), emit)
    goal = ("Fix gcd in python_programs/gcd.py. Preserve its public interface. "
            "Inspect the source before editing. Only modify this file. "
            "You may run Python commands to check your change. "
            "Describe the change when finished. No tests are included in this workspace.")
    context = ExecutionContext(output.name, goal, 8)
    emit("[task] " + goal)
    emit("[budget] 8 steps, 2048 output tokens/request, 240 seconds total, no run retry")
    started = time.monotonic()
    previous_cwd = Path.cwd()
    logging.getLogger("loopsmith.core.loop").setLevel(logging.CRITICAL)
    try:
        os.chdir(workspace)
        async with EventWriter(output / "events.jsonl") as writer:
            writer.subscribe(bus)
            try:
                async with asyncio.timeout(240):
                    await AgentLoop(provider, registry, bus).run(context)
            except TimeoutError:
                context.mark_failed("pilot_timeout")
    finally:
        os.chdir(previous_cwd)
    changed = candidate.read_text(encoding="utf-8")
    (output / "after.py").write_text(changed, encoding="utf-8")
    (output / "changes.diff").write_text("".join(difflib.unified_diff(
        original.splitlines(keepends=True), changed.splitlines(keepends=True),
        fromfile="a/python_programs/gcd.py", tofile="b/python_programs/gcd.py")), encoding="utf-8")
    result: dict[str, Any] = {
        **config, "agent_status": context.status, "reason": context.reason,
        "steps": context.step, "api_calls": provider.calls, "api_error": provider.error,
        "usage_complete": len(usage) == provider.calls,
        "input_tokens": sum(u["input_tokens"] for u in usage),
        "output_tokens": sum(u["output_tokens"] for u in usage),
        "agent_elapsed_s": round(time.monotonic() - started, 3),
        "answer": context.result, "changed": changed != original,
    }
    write_json(output / "result.json", result)
    if provider.error is None and result["changed"]:
        emit("[grader] running independent local QuixBugs tests; no additional model calls")
        result["grading"] = grade_quix(ROOT / ".eval-data", ROOT / ".eval-data/frozen-v1",
                                        "gcd", candidate, output / "grading", timeout=120)
        write_json(output / "result.json", result)
        emit("[grader] " + json.dumps(result["grading"]))
    emit(f"[done] agent={context.status}, calls={provider.calls}, changed={result['changed']}")
    if context.result:
        emit("[answer] " + context.result[:1600])
    log.close()


if __name__ == "__main__":
    asyncio.run(main())
