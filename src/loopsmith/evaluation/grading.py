from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from loopsmith.evaluation.data import sha256, verify, write_json


# 解析外部 pytest 报告，空测试、跳过和收集失败都不能视作修复成功
def parse_junit(path: Path, returncode: int) -> dict[str, Any]:
    if not path.exists():
        return {"status": "test_error", "tests": 0, "failures": 0, "errors": 1, "skipped": 0}
    tree = ET.parse(path)
    cases = tree.findall(".//testcase")
    counts = {
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }
    if (
        returncode == 0
        and counts["tests"] > 0
        and not any(counts[key] for key in ("failures", "errors", "skipped"))
    ):
        status = "passed"
    elif returncode == 1 and counts["failures"] > 0 and not counts["errors"]:
        status = "failed"
    else:
        status = "test_error"
    return {"status": status, **counts}


# 使用最小环境启动本地测试，避免把宿主机密钥传给候选代码
def test_environment(work: Path) -> dict[str, str]:
    keep = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT")
    env = {key: value for key, value in os.environ.items() if key.upper() in keep}
    env.update(
        {
            "PYTHONPATH": str(work),
            "PYTHONIOENCODING": "utf-8",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )
    return env


# 在独立临时目录运行可信测试，仅接收单个候选源码；本地进程隔离不等于安全沙箱
def grade_quix(
    root: Path,
    prepared: Path,
    name: str,
    candidate: Path,
    report_dir: Path,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    manifest = verify(root, prepared)
    if name not in {task["program"] for task in manifest["quixbugs"]}:
        raise ValueError("Task is not in the frozen development set")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("Candidate must be a regular Python source file")
    report_dir.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {
        "dataset": "quixbugs",
        "instance_id": f"quixbugs-{name}",
        "candidate_sha256": sha256(candidate),
        "timeout_s": timeout,
    }
    source = root / "quixbugs"
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="work-", dir=report_dir) as temp:
        work = Path(temp)
        if not work.resolve().is_relative_to(report_dir.resolve()):
            raise ValueError("Grading work directory escaped report directory")
        for folder in ("python_programs", "json_testcases"):
            (work / folder).mkdir()
        shutil.copy2(source / "json_testcases" / f"{name}.json",
                     work / "json_testcases" / f"{name}.json")
        tests_dir = work / "python_testcases"
        tests_dir.mkdir()
        for filename in (f"test_{name}.py", "load_testdata.py"):
            shutil.copy2(source / "python_testcases" / filename, tests_dir / filename)
        shutil.copy2(source / "conftest.py", work / "conftest.py")
        shutil.copy2(candidate, work / "python_programs" / f"{name}.py")
        command = [
            getattr(sys, "_base_executable", sys.executable),
            "-c",
            "import sys; sys.path.insert(0, " + repr(sysconfig.get_path("purelib")) + "); "
            "import pytest; raise SystemExit(pytest.main(sys.argv[1:]))",
            "-q",
            "--tb=line",
            "-p",
            "no:cacheprovider",
            f"python_testcases/test_{name}.py",
            "--junitxml=result.xml",
        ]
        try:
            proc = subprocess.run(
                command, cwd=work, env=test_environment(work), capture_output=True, timeout=timeout
            )
            log = proc.stdout + proc.stderr
            result.update(parse_junit(work / "result.xml", proc.returncode))
            result["returncode"] = proc.returncode
            if (work / "result.xml").exists():
                shutil.copy2(work / "result.xml", report_dir / "junit.xml")
        except subprocess.TimeoutExpired as exc:
            log = (exc.stdout or b"") + (exc.stderr or b"")
            result.update(status="timeout", returncode=None)
        (report_dir / "test.log").write_bytes(log)
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    verify(root, prepared)
    write_json(report_dir / "result.json", result)
    return result


# 用缺陷和参考修复分别验证评分器，结果不计入 Agent 成绩
def preflight_quix(
    root: Path, prepared: Path, reports: Path, timeout: int, *, resume: bool = False,
) -> dict[str, Any]:
    results = []
    manifest = verify(root, prepared)
    for task in manifest["quixbugs"]:
        name = task["program"]
        pair = {}
        for label, folder in (
            ("buggy", "python_programs"),
            ("reference", "correct_python_programs"),
        ):
            candidate = root / "quixbugs" / folder / f"{name}.py"
            report = reports / name / label
            previous = report / "result.json"
            if resume and previous.exists():
                saved = json.loads(previous.read_text(encoding="utf-8"))
                if (saved.get("candidate_sha256") == sha256(candidate)
                        and saved.get("status") == ("failed" if label == "buggy" else "passed")
                        and (report / "junit.xml").exists()
                        and parse_junit(report / "junit.xml", saved["returncode"])["status"]
                        == saved["status"]):
                    pair[label] = saved
                    continue
            if report.exists():
                raise FileExistsError(
                    f"Unverified existing result: {report}; use a new report directory"
                )
            pair[label] = grade_quix(
                root,
                prepared,
                name,
                candidate,
                report,
                timeout=timeout,
            )
        results.append(
            {
                "program": name,
                "eligible": pair["buggy"]["status"] == "failed"
                and pair["reference"]["status"] == "passed",
                **pair,
            }
        )
        print(
            f"{name}: buggy={pair['buggy']['status']}, reference={pair['reference']['status']}",
            flush=True,
        )
    summary = {
        "kind": "grader_preflight_not_agent_evaluation",
        "results": results,
        "eligible": sum(item["eligible"] for item in results),
        "total": len(results),
    }
    write_json(reports / "preflight.json", summary)
    return summary


# 生成标准 SWE-bench 预测文件，拒绝未知任务、重复任务与空补丁
def export_predictions(
    root: Path,
    prepared: Path,
    patches: Path,
    output: Path,
    model_name: str,
) -> int:
    manifest = verify(root, prepared)
    allowed = {task["instance_id"] for task in manifest["swe_bench"]}
    entries = []
    for path in sorted(patches.glob("*.patch")):
        if path.stem not in allowed:
            raise ValueError(f"Unknown instance: {path.stem}")
        patch = path.read_text(encoding="utf-8")
        if not patch.strip().startswith("diff --git "):
            raise ValueError(f"Expected a non-empty git diff: {path.name}")
        entries.append(
            {"instance_id": path.stem, "model_name_or_path": model_name, "model_patch": patch}
        )
    if not entries:
        raise ValueError("No predictions found")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(item) + "\n" for item in entries), encoding="utf-8")
    return len(entries)
