from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from loopsmith.evaluation.data import sha256, verify, write_json


# 优先使用项目内独立的验收环境，避免向主运行时安装重型评测依赖
def harness_python() -> str:
    explicit = os.environ.get("LOOPSMITH_SWEBENCH_PYTHON")
    if explicit:
        return str(Path(explicit).resolve())
    relative = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    dedicated = Path(".eval-data/swe-env") / relative
    return str(dedicated.resolve()) if dedicated.exists() else sys.executable


# 检查本机验收依赖，只检查 Docker 服务，不拉取镜像
def doctor() -> dict[str, Any]:
    interpreter = harness_python()
    version: str | None = None
    if interpreter == sys.executable:
        try:
            version = importlib.metadata.version("swebench")
        except importlib.metadata.PackageNotFoundError:
            pass
    else:
        try:
            check = subprocess.run(
                [
                    interpreter,
                    "-c",
                    "import importlib.metadata; print(importlib.metadata.version('swebench'))",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if check.returncode == 0:
                version = check.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        docker_ready = result.returncode == 0
        docker_version = result.stdout.strip() if docker_ready else None
    except (OSError, subprocess.TimeoutExpired):
        docker_ready, docker_version = False, None
    return {
        "swebench_version": version,
        "harness_python": interpreter,
        "docker_ready": docker_ready,
        "docker_version": docker_version,
        "ready": bool(version and docker_ready),
    }


# 生成固定版本的本地 harness 数据与验收命令，默认仅准备而不执行
def grade_swe(
    root: Path,
    prepared: Path,
    reports: Path,
    *,
    mode: str,
    predictions: Path | None = None,
    instance_id: str | None = None,
    execute: bool = False,
    harness_timeout_s: int = 300,
) -> dict[str, Any]:
    manifest = verify(root, prepared)
    rows = json.loads((root / "swe-bench-verified.json").read_text(encoding="utf-8"))
    allowed = {task["instance_id"] for task in manifest["swe_bench"]}
    if instance_id:
        if instance_id not in allowed:
            raise ValueError("Instance is not in frozen selection")
        allowed = {instance_id}
    selected = [row for row in rows if row["instance_id"] in allowed]
    reports.mkdir(parents=True, exist_ok=False)
    dataset_path = reports / "grader-only-dataset.json"
    write_json(dataset_path, selected)
    if mode == "candidate":
        if predictions is None:
            raise ValueError("Candidate grading requires --predictions")
        entries = [
            json.loads(line)
            for line in predictions.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        ids = [entry["instance_id"] for entry in entries]
        if len(ids) != len(set(ids)) or set(ids) != allowed:
            raise ValueError("Predictions must cover exactly the selected IDs without duplicates")
    else:
        # 负对照只添加无关文本文件，保留原始缺陷；空补丁会被官方 harness 跳过
        negative = (
            "diff --git a/loopsmith_preflight.txt b/loopsmith_preflight.txt\n"
            "new file mode 100644\n--- /dev/null\n+++ b/loopsmith_preflight.txt\n"
            "@@ -0,0 +1 @@\n+negative control, no source modification\n"
        )
        entries = [
            {
                "instance_id": row["instance_id"],
                "model_name_or_path": f"preflight-{mode}",
                "model_patch": row["patch"] if mode == "reference" else negative,
            }
            for row in selected
        ]
    predictions_path = reports / "predictions.jsonl"
    predictions_path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    run_id = f"loopsmith-{mode}-{reports.name}"
    command = [
        harness_python(),
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        str(dataset_path),
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        "1",
        "--timeout",
        str(harness_timeout_s),
        "--run_id",
        run_id,
    ]
    result: dict[str, Any] = {
        "kind": "agent_evaluation" if mode == "candidate" else "environment_preflight",
        "mode": mode,
        "instance_ids": sorted(allowed),
        "command": command,
        "dataset_sha256": sha256(dataset_path),
        "predictions_sha256": sha256(predictions_path),
        "environment": doctor(),
        "status": "prepared_not_executed",
        "expected": "resolved"
        if mode == "reference"
        else "unresolved"
        if mode == "negative"
        else None,
    }
    write_json(reports / "invocation.json", result)
    if not execute:
        return result
    if not result["environment"]["ready"]:
        result["status"] = "environment_unavailable"
        write_json(reports / "invocation.json", result)
        raise RuntimeError("SWE harness/Docker unavailable; see invocation.json")
    # 保留代理和 Docker 配置，但不向测试 harness 传入模型 API 密钥
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    # Windows 的默认 GBK 会误解码含 Unicode 的补丁；只固定验收子进程编码
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    with (reports / "harness.log").open("wb") as log:
        process = subprocess.run(
            command, cwd=reports, env=env, stdout=log, stderr=subprocess.STDOUT
        )
    result["harness_returncode"] = process.returncode
    outcomes: dict[str, Any] = {}
    for path in (reports / "logs" / "run_evaluation" / run_id).rglob("report.json"):
        content = json.loads(path.read_text(encoding="utf-8"))
        for identity, report in content.items():
            if identity in allowed:
                outcomes[identity] = report
    result["outcomes"] = outcomes
    complete = process.returncode == 0 and set(outcomes) == allowed
    if not complete:
        result["status"] = "environment_or_harness_error"
    elif mode in ("reference", "negative"):
        expected = mode == "reference"
        result["status"] = (
            "preflight_passed"
            if all(report.get("resolved") is expected for report in outcomes.values())
            else "preflight_failed"
        )
    else:
        result["status"] = "graded"
        result["resolved"] = sum(report.get("resolved") is True for report in outcomes.values())
    verify(root, prepared)
    write_json(reports / "invocation.json", result)
    return result
