from __future__ import annotations

import json
import re
import shlex
import uuid
from pathlib import Path
from typing import Any

from loopsmith.core.tools.base import BaseTool, ToolResult

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_COUNT = re.compile(r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")


# 解析真实测试摘要，不把读取源码、空测试、收集错误或管道退出码零当成通过
def test_feedback(returncode: int, output: str) -> dict[str, Any]:
    text = _ANSI.sub("", output)
    counts: dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.strip(" =")
        if not (
            re.match(r"^\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed)", stripped)
            or re.match(r"^tests finished:\s*\d+", stripped, re.I)
        ):
            continue
        current = {
            key.rstrip("s") if key.startswith("error") else key: int(count)
            for count, key in _COUNT.findall(stripped)
        }
        if current:
            counts = current
    ran = re.findall(r"^Ran (\d+) tests? in .+$", text, re.M)
    unittest_ok = bool(re.search(r"^OK(?:\s*\([^\n]*\))?\s*$", text, re.M))
    if ran:
        total = int(ran[-1])
        failures = re.findall(r"^FAILED \(failures=(\d+)", text, re.M)
        errors = re.findall(r"errors=(\d+)", text)
        skipped = re.findall(r"skipped=(\d+)", text)
        skipped_count = int(skipped[-1]) if skipped else 0
        if unittest_ok:
            counts = {"passed": max(0, total - skipped_count), "skipped": skipped_count}
        elif failures or errors:
            counts = {
                "failed": int(failures[-1]) if failures else 0,
                "error": int(errors[-1]) if errors else 0,
                "skipped": skipped_count,
            }
            counts["passed"] = max(0, total - sum(counts.values()))
    executed = sum(
        counts.get(key, 0) for key in ("passed", "failed", "error", "xfailed", "xpassed")
    )
    if returncode == 124:
        status = "timeout"
    elif returncode == 137:
        status = "killed_or_timeout"
    elif returncode == 5 or (counts and executed == 0):
        status = "no_tests"
    elif counts.get("error", 0) or returncode == 2:
        status = "collection_or_runner_error"
    elif counts.get("failed", 0):
        status = "failed"
    elif returncode == 0 and counts.get("passed", 0) > 0:
        status = "passed"
    else:
        status = "unknown"
    return {
        "status": status,
        "returncode": returncode,
        "tests_executed": executed,
        "counts": counts,
        "valid_test_run": executed > 0 and status in ("passed", "failed"),
    }


class RunTestsTool(BaseTool):
    name = "run_tests"
    description = (
        "Run the project's fixed verification command. Returns real test status and counts, "
        "not a shell pipeline's success. Prefer this for regression checks. "
        "Passing existing tests does not by itself prove the reported bug is fixed."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    # 保存可信测试命令、容器和守卫，不让模型替换测试命令或弱化断言
    def __init__(
        self, container: Any, command: str, guard: Any, log_dir: Path | None = None
    ) -> None:
        self.container, self.command, self.guard = container, command, guard
        self.log_dir = log_dir

    # 将完整输出留在容器日志，只回传摘要和尾部；保留真实退出码
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        log = f"/{'root' if self.log_dir else 'tmp'}/loopsmith-test-{uuid.uuid4().hex}.log"
        fingerprint = getattr(self.guard, "fingerprint", None)
        before = await fingerprint() if fingerprint else None
        marker = "__LOOPSMITH_TEST_RC__="
        command = (
            f"set -o pipefail; ({self.command}) > {shlex.quote(log)} 2>&1; "
            "rc=$?; "
            f"tail -c 16000 -- {shlex.quote(log)}; "
            f'printf \'\\n{marker}%s\\n\' "$rc"; exit "$rc"'
        )
        rc, output = await self.container.exec(command)
        match = re.search(re.escape(marker) + r"(\d+)\s*$", output)
        if match:
            rc = int(match.group(1))
            output = output[: match.start()]
        feedback = test_feedback(rc, output)
        if fingerprint:
            after = await fingerprint()
            feedback["candidate_fingerprint"] = before if before == after else None
            if before != after:
                feedback["valid_test_run"] = False
                feedback["status"] = "candidate_changed_during_tests"
        self.guard.note_test(feedback)
        summary = {**feedback, "command": self.command, "full_log_in_container": log}
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.container.copy_out(log, self.log_dir / Path(log).name)
            except (Exception, SystemExit):
                summary["log_persistence_error"] = True
        return ToolResult(
            json.dumps(summary, ensure_ascii=False) + "\n" + output[-10000:],
            feedback["status"] != "passed",
            "test_failed" if feedback["status"] != "passed" else None,
        )
