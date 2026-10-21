from __future__ import annotations

import ast
import hashlib
import json
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loopsmith.core.tools.base import BaseTool, ToolResult

_FINGERPRINT_SCRIPT = r"""
import hashlib, os, stat, subprocess
h = hashlib.sha256()

# 按内容和可执行模式记录当前候选，忽略 Git 忽略的运行缓存，递归记录子模块
def collect(root, prefix=""):
    names = subprocess.check_output([
        "git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"
    ], cwd=root).split(b"\0")
    for raw in sorted(set(names)):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = os.path.join(root, name)
        relative = prefix + name
        h.update(os.fsencode(relative) + b"\0")
        if not os.path.lexists(path):
            h.update(b"deleted")
        elif os.path.islink(path):
            h.update(b"link" + os.fsencode(os.readlink(path)))
        elif os.path.isdir(path):
            h.update(b"directory")
            if os.path.exists(os.path.join(path, ".git")):
                collect(path, relative + "/")
        else:
            h.update(str(os.stat(path).st_mode & 0o111).encode("ascii"))
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    h.update(chunk)
collect("/testbed")
print(h.hexdigest())
"""


# 对候选内容而非命令正则取指纹，任何编辑都会使旧验证证据失效
async def candidate_fingerprint(container: Any) -> str:
    rc, output = await container.exec_bytes(
        "python -", stdin=_FINGERPRINT_SCRIPT.encode("utf-8"), timeout_s=90
    )
    value = output.strip().splitlines()[-1] if output.strip() else ""
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("Cannot fingerprint candidate source")
    return value


class IssueReproducerTool(BaseTool):
    name = "check_issue"
    description = (
        "Before editing: supply a small Python script with assertions for the issue. "
        "A failing baseline is expected. After editing: call with no script to rerun "
        "EXACTLY the same assertions. Uses the task's Python environment and /testbed cwd. "
        "Use synthetic data and temporary directories, not downloads. "
        "Does not replace regression tests."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "Initial assertion script; omit on reruns.",
            },
            "explain_test_failures": {
                "type": "string",
                "description": (
                    "If old tests fail due to behavior explicitly changed by the issue, "
                    "explain each mismatch. Never excuse unrelated regressions."
                ),
            },
        },
        "additionalProperties": False,
    }

    # 固定复现脚本与起始候选，记录真实基线和修复后执行结果
    def __init__(
        self,
        container: Any,
        initial_fingerprint: str,
        fingerprint: Callable[[], Awaitable[str]],
        artifact_dir: Path,
    ) -> None:
        self.container = container
        self.initial_fingerprint = initial_fingerprint
        self.fingerprint = fingerprint
        self.artifact_dir = artifact_dir
        self.script: str | None = None
        self.script_sha256: str | None = None
        self.baseline_returncode: int | None = None
        self.last_returncode: int | None = None
        self.verified_fingerprint: str | None = None
        self.explanation = ""
        self.attempts = 0

    # 首次要求问题在干净基线上失败，后续只执行同一脚本并绑定当前代码指纹
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        supplied = params.get("script")
        if self.script is not None and supplied is not None and supplied != self.script:
            return ToolResult(
                "Reproducer is immutable after baseline confirmation. Omit script.",
                True,
                "reproducer_conflict",
            )
        script = self.script or supplied
        if not isinstance(script, str) or not script.strip() or len(script.encode("utf-8")) > 24000:
            return ToolResult(
                "Supply an assertion-based Python script (at most 24 KB).", True, "schema_error"
            )
        try:
            tree = ast.parse(script)
        except SyntaxError:
            return ToolResult("Reproducer has invalid Python syntax.", True, "schema_error")
        if not any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
            return ToolResult(
                "Use assertions on the expected behavior; printing is insufficient.",
                True,
                "schema_error",
            )
        before = await self.fingerprint()
        baseline = self.script is None
        if baseline and before != self.initial_fingerprint:
            return ToolResult(
                "Baseline must be recorded before source edits. Preserve your patch, restore "
                "the initial source, record the failing baseline, then reapply your patch.",
                True,
                "baseline_already_modified",
            )
        rc, output = await self.container.exec_bytes(
            "cat > /tmp/loopsmith-issue-reproducer.py && PYTHONDONTWRITEBYTECODE=1 python -c "
            + shlex.quote(
                'import runpy,sys; sys.path.insert(0,"/testbed"); '
                'runpy.run_path("/tmp/loopsmith-issue-reproducer.py",run_name="__main__")'
            ),
            stdin=script.encode("utf-8"),
            timeout_s=90,
            output_limit=24000,
        )
        after = await self.fingerprint()
        self.attempts += 1
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        (self.artifact_dir / f"attempt-{self.attempts}.log").write_text(output, encoding="utf-8")
        self.last_returncode = rc
        if before != after:
            self.verified_fingerprint = None
            return ToolResult(
                "Reproducer changed candidate files. Use temporary directories "
                "and keep source unchanged.",
                True,
                "reproducer_modified_source",
            )
        if baseline:
            invalid = rc in (124, 137) or re.search(
                r"(?:ModuleNotFoundError|ImportError|SyntaxError):", output
            )
            if rc == 0 or invalid:
                return ToolResult(
                    "Baseline did not demonstrate the reported failure (or failed during setup). "
                    "Correct the reproducer; a green baseline proves no fix.\n"
                    + output[-6000:],
                    True,
                    "baseline_not_reproduced",
                )
            self.script = script
            self.script_sha256 = hashlib.sha256(script.encode("utf-8")).hexdigest()
            self.baseline_returncode = rc
            (self.artifact_dir / "reproducer.py").write_text(script, encoding="utf-8")
            status = "baseline_failure_observed"
        else:
            self.verified_fingerprint = after if rc == 0 else None
            self.explanation = (
                str(params.get("explain_test_failures") or "").strip() if rc == 0 else ""
            )
            status = "issue_assertions_passed" if rc == 0 else "issue_assertions_failed"
        return ToolResult(
            json.dumps(
                {
                    "status": status,
                    "returncode": rc,
                    "script_sha256": self.script_sha256,
                    "candidate_fingerprint": after,
                },
                ensure_ascii=False,
            )
            + "\n"
            + output[-6000:],
            not baseline and rc != 0,
            "issue_assertions_failed" if not baseline and rc != 0 else None,
        )
