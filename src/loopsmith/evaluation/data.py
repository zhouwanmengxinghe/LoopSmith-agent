from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

SWE_DATASET = "SWE-bench/SWE-bench_Verified"
QUIX_TASKS = (
    "find_in_sorted",
    "flatten",
    "gcd",
    "get_factors",
    "is_valid_parenthesization",
    "next_permutation",
    "lis",
    "max_sublist_sum",
    "mergesort",
    "rpn_eval",
)


# 计算文件内容哈希，供冻结和验收时核对
def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 写入格式稳定的 JSON 文件
def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# 获取公开数据，强制经过用户代理且保持 TLS 校验
def fetch_json(url: str) -> Any:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        raise RuntimeError("Set HTTPS_PROXY before downloading public datasets")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}))
    with opener.open(url, timeout=120) as response:
        return json.load(response)


# 下载 Verified 元数据快照，不下载模型、不调用 API 推理
def fetch_swe(root: Path) -> Path:
    destination = root / "swe-bench-verified.json"
    if destination.exists():
        raise FileExistsError("Snapshot exists; use a new data directory to refresh it")
    rows: list[dict[str, Any]] = []
    total = 1
    while len(rows) < total:
        query = urllib.parse.urlencode(
            {
                "dataset": SWE_DATASET,
                "config": "default",
                "split": "test",
                "offset": len(rows),
                "length": 100,
            }
        )
        page = fetch_json("https://datasets-server.huggingface.co/rows?" + query)
        batch = [item["row"] for item in page["rows"]]
        total = page["num_rows_total"]
        if not batch:
            raise ValueError("Incomplete dataset download")
        rows.extend(batch)
    if len(rows) != 500 or len({row["instance_id"] for row in rows}) != len(rows):
        raise ValueError("Expected 500 unique Verified instances; review upstream changes")
    write_json(destination, rows)
    return destination


# 对任务按仓库轮转与固定哈希排序，避免依据 Agent 成败挑题
def select_swe(rows: list[dict[str, Any]], count: int, seed: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["repo"], []).append(row)
    for group in groups.values():
        group.sort(
            key=lambda row: hashlib.sha256(
                f"{seed}:{row['instance_id']}".encode(),
            ).hexdigest()
        )
    selected: list[dict[str, Any]] = []
    while len(selected) < count:
        progressed = False
        for repo in sorted(groups):
            if groups[repo] and len(selected) < count:
                selected.append(groups[repo].pop(0))
                progressed = True
        if not progressed:
            raise ValueError("Not enough unique tasks")
    return selected


# 冻结数据与候选清单，公开任务文件不含补丁和评测答案
def prepare(root: Path, output: Path, *, count: int = 30, seed: int = 20261018) -> dict[str, Any]:
    if not 1 <= count <= 500:
        raise ValueError("SWE subset size must be between 1 and 500")
    if output.exists():
        raise FileExistsError("Prepared directory exists; do not overwrite frozen tasks")
    quix = root / "quixbugs"
    snapshot = root / "swe-bench-verified.json"
    rows = json.loads(snapshot.read_text(encoding="utf-8"))
    commit = subprocess.check_output(
        ["git", "-C", str(quix), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "seed": seed,
        "quixbugs_commit": commit,
        "swe_dataset": SWE_DATASET,
        "swe_snapshot_sha256": sha256(snapshot),
        "selection": "repository round-robin, SHA256(seed:instance_id)",
        "status": "candidate_set_environment_preflight_required",
        "quixbugs": [],
        "swe_bench": [],
        "protected_hashes": {},
    }
    for folder in (
        "python_programs",
        "correct_python_programs",
        "python_testcases",
        "json_testcases",
    ):
        for path in sorted((quix / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                manifest["protected_hashes"][path.relative_to(root).as_posix()] = sha256(path)
    manifest["protected_hashes"]["quixbugs/conftest.py"] = sha256(quix / "conftest.py")
    for name in QUIX_TASKS:
        task_dir = output / "agent" / "quixbugs" / name
        shutil.copytree(
            quix / "python_programs",
            task_dir / "python_programs",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        task = {
            "instance_id": f"quixbugs-{name}",
            "program": name,
            "split": "development",
            "editable_path": f"python_programs/{name}.py",
            "goal": f"Fix {name} in python_programs/{name}.py. Preserve its public interface.",
        }
        write_json(task_dir / "task.json", task)
        manifest["quixbugs"].append(task)
    for row in select_swe(rows, count, seed):
        task = {
            key: row[key] for key in ("instance_id", "repo", "base_commit", "problem_statement")
        }
        task["split"] = "evaluation"
        task["environment_status"] = "not_checked"
        manifest["swe_bench"].append(task)
        write_json(output / "agent" / "swe-bench" / f"{task['instance_id']}.json", task)
    write_json(output / "manifest.json", manifest)
    (output / "manifest.sha256").write_text(
        sha256(output / "manifest.json") + "\n", encoding="ascii"
    )
    return manifest


# 导出不含参考补丁和问题正文的可版本管理任务索引
def inventory(root: Path, prepared: Path, destination: Path) -> dict[str, Any]:
    manifest = verify(root, prepared)
    result = {
        key: manifest[key]
        for key in (
            "schema_version",
            "seed",
            "quixbugs_commit",
            "swe_dataset",
            "swe_snapshot_sha256",
            "selection",
            "status",
        )
    }
    result["manifest_sha256"] = sha256(prepared / "manifest.json")
    result["quixbugs"] = manifest["quixbugs"]
    result["swe_bench"] = [
        {
            key: task[key]
            for key in (
                "instance_id",
                "repo",
                "base_commit",
                "split",
                "environment_status",
            )
        }
        for task in manifest["swe_bench"]
    ]
    if destination.exists():
        raise FileExistsError(destination)
    write_json(destination, result)
    return result


# 验证冻结的清单、源数据与外部测试均未变化
def verify(root: Path, output: Path) -> dict[str, Any]:
    if sha256(output / "manifest.json") != (output / "manifest.sha256").read_text(
        encoding="utf-8"
    ).strip():
        raise ValueError("Manifest changed after freeze")
    manifest = cast(
        dict[str, Any],
        json.loads(
            (output / "manifest.json").read_text(encoding="utf-8"),
        ),
    )
    if sha256(root / "swe-bench-verified.json") != manifest["swe_snapshot_sha256"]:
        raise ValueError("SWE snapshot changed")
    for relative, expected in manifest["protected_hashes"].items():
        if sha256(root / relative) != expected:
            raise ValueError(f"Protected benchmark artifact changed: {relative}")
    return manifest
