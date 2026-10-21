"""SWE-bench Verified 批量基线：跑多题、收指标、出对照表。

设计取向：
- 每題仍然走 `run_swe_baseline.py`（独立进程、独立工作区、独立事件流），
  批量层只负责编排、汇总和官方验收，避免把执行细节复制一份。
- 只对本地已有官方镜像的题目执行；缺镜像的记为 image_missing 并从分母中单独统计，
  不静默丢掉，也不隐式触发大体积下载。
- 指标分三层：过程（步数/调用/token/耗时）、产物（补丁大小/是否产生）、
  结果（官方 harness 的 resolved 与 FAIL_TO_PASS/PASS_TO_PASS 明细）。

默认只做计划不执行；加 --execute 才调用模型。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from loopsmith.evaluation.data import verify, write_json  # noqa: E402

RUNNER = ROOT / "scripts" / "run_swe_baseline.py"
EVALUATE = ROOT / "scripts" / "evaluate.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
DATA = ROOT / ".eval-data"


def frozen_instances() -> list[dict[str, Any]]:
    manifest = verify(DATA, DATA / "frozen-v1")
    return list(manifest["swe_bench"])


def dataset_image(instance_id: str) -> str:
    rows = json.loads((DATA / "swe-bench-verified.json").read_text(encoding="utf-8"))
    for row in rows:
        if row["instance_id"] == instance_id:
            return str(row["image"])
    raise SystemExit(f"{instance_id} 不在本地数据集里")


def image_ready(image: str) -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True
        ).returncode
        == 0
    )


def run_one(instance_id: str, out_dir: Path, budget: dict[str, Any]) -> dict[str, Any]:
    """跑一题；输出目录已存在则表示本题之前跑过，直接复用结果。

    目录存在但没有 result.json，说明上一次运行被中断（进程被杀/异常退出）。
    这时把残留目录改名留存，再开一个新的重跑，避免"拒绝覆盖"被记成失败。
    """
    if (out_dir / "result.json").exists():
        return json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    if out_dir.exists():
        broken = out_dir.with_name(f"{instance_id}.broken-{int(time.time())}")
        out_dir.rename(broken)
    command = [
        str(PYTHON),
        str(RUNNER),
        "--instance-id",
        instance_id,
        "--output",
        str(out_dir),
        "--max-steps",
        str(budget["max_steps"]),
        "--max-output-tokens",
        str(budget["max_output_tokens"]),
        "--run-timeout-s",
        str(budget["run_timeout_s"]),
        "--request-interval-s",
        str(budget["request_interval_s"]),
        "--execute",
    ]
    log_path = out_dir.parent / f"{instance_id}.runner.log"
    if budget.get("thinking_enabled"):
        command.extend(["--thinking-enabled", "--thinking-effort", budget.get("thinking_effort", "low")])
    command.extend(["--repair-policy", budget.get("repair_policy", "feedback")])
    started = time.monotonic()
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if (out_dir / "result.json").exists():
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
        result["runner_returncode"] = proc.returncode
        result["runner_wall_s"] = round(time.monotonic() - started, 1)
        return result
    return {
        "instance_id": instance_id,
        "agent_status": "runner_crashed",
        "reason": f"returncode={proc.returncode}",
        "runner_returncode": proc.returncode,
        "log": str(log_path),
    }


def write_predictions(run_dir: Path, instance_id: str, model_name: str) -> Path:
    """补丁为空时也写入预测，保证每题都在分母里（官方 harness 会判为未解决）。"""
    patch_file = run_dir / "patches" / f"{instance_id}.patch"
    patch = patch_file.read_text(encoding="utf-8") if patch_file.exists() else ""
    target = run_dir / "predictions.jsonl"
    target.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "model_name_or_path": model_name,
                "model_patch": patch,
            }
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return target


def grade_one(
    run_dir: Path,
    instance_id: str,
    batch_id: str,
    model_name: str,
    harness_timeout_s: int = 300,
) -> dict[str, Any]:
    predictions = write_predictions(run_dir, instance_id, model_name)
    # 空补丁不能直接扔给官方 harness：它会把整个实例跳过，只回一个
    # environment_or_harness_error 和空的 outcomes，于是"没做出补丁"这道题
    # 反而从分母里消失、解决率被抬高。语义上没产出补丁就是没解决，直接判 false。
    patch_path = run_dir / "patches" / f"{instance_id}.patch"
    if not patch_path.exists() or not patch_path.read_text(encoding="utf-8").strip():
        return {
            "grading_status": "no_patch",
            "resolved": False,
            "patch_applied": False,
            "f2p_success": 0,
            "f2p_failure": 0,
            "p2p_failure": 0,
            "reports": None,
        }
    reports = DATA / "reports" / f"{batch_id}-{instance_id}"
    # 复用的前提是"上一次真的验收完了"。历史报告可能留下多份（每次失败换一个
    # r2/r3... 后缀），所以要**在所有候选里找 status=graded 的那一份**，而不是
    # 简单地取"下一个空号"—— 后者会无视已成功的报告重新跑一遍，慢题因此反复超时。
    candidates = [reports]
    candidates += sorted(
        (DATA / "reports").glob(f"{batch_id}-{instance_id}-r*"),
        key=lambda p: int(p.name.rsplit("-r", 1)[-1]) if p.name.rsplit("-r", 1)[-1].isdigit() else 0,
    )
    graded_dir: Path | None = None
    saw_timeout = False
    for candidate in candidates:
        invocation = candidate / "invocation.json"
        if not invocation.exists():
            continue
        payload = json.loads(invocation.read_text(encoding="utf-8"))
        if payload.get("status") == "graded":
            graded_dir = candidate
            break
        # 记录到过测试超时：说明这题套件比默认 300s 慢，重试要放宽
        for report in candidate.glob("*.json"):
            if report.name in ("invocation.json", "grader-only-dataset.json"):
                continue
            if "tests_timed_out" in report.read_text(encoding="utf-8", errors="replace"):
                saw_timeout = True
                break

    if graded_dir is not None:
        reports = graded_dir
    else:
        if saw_timeout and harness_timeout_s <= 300:
            harness_timeout_s = 900
        suffix = 2
        while (DATA / "reports" / f"{batch_id}-{instance_id}-r{suffix}").exists():
            suffix += 1
        reports = DATA / "reports" / f"{batch_id}-{instance_id}-r{suffix}"
    if not reports.exists() or not (reports / "invocation.json").exists():
        command = [
            str(PYTHON),
            str(EVALUATE),
            "grade-swe",
            "--mode",
            "candidate",
            "--instance-id",
            instance_id,
            "--predictions",
            str(predictions),
            "--reports",
            str(reports),
            "--execute",
        ]
        if harness_timeout_s != 300:
            command += ["--harness-timeout", str(harness_timeout_s)]
        log = run_dir.parent / f"{instance_id}.grade.log"
        with log.open("a", encoding="utf-8") as handle:
            proc = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT)
        if not (reports / "invocation.json").exists():
            return {"grading_status": "failed", "grading_returncode": proc.returncode}
    invocation = json.loads((reports / "invocation.json").read_text(encoding="utf-8"))
    outcome = (invocation.get("outcomes") or {}).get(instance_id, {})
    tests = outcome.get("tests_status") or {}
    return {
        "grading_status": invocation.get("status"),
        "resolved": outcome.get("resolved"),
        "patch_applied": outcome.get("patch_successfully_applied"),
        "f2p_success": len((tests.get("FAIL_TO_PASS") or {}).get("success") or []),
        "f2p_failure": len((tests.get("FAIL_TO_PASS") or {}).get("failure") or []),
        "p2p_failure": len((tests.get("PASS_TO_PASS") or {}).get("failure") or []),
        "reports": str(reports.relative_to(ROOT)),
    }


def patch_files(run_dir: Path, instance_id: str) -> list[str]:
    path = run_dir / "patches" / f"{instance_id}.patch"
    if not path.exists():
        return []
    return [
        line.split(" b/")[0][len("diff --git a/") :]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("diff --git a/")
    ]


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    executed = [row for row in rows if row.get("agent_status")]
    graded = [row for row in rows if row.get("resolved") is not None]
    resolved = [row for row in graded if row["resolved"]]

    def stats(key: str) -> dict[str, float] | None:
        values = [row[key] for row in executed if isinstance(row.get(key), (int, float))]
        if not values:
            return None
        return {
            "mean": round(statistics.fmean(values), 1),
            "median": round(statistics.median(values), 1),
            "min": min(values),
            "max": max(values),
        }

    reasons: dict[str, int] = {}
    for row in executed:
        reason = str(row.get("reason") or row.get("agent_status"))
        reasons[reason] = reasons.get(reason, 0) + 1

    return {
        "tasks_planned": len(rows),
        "tasks_executed": len(executed),
        "tasks_graded": len(graded),
        "resolved": len(resolved),
        "resolve_rate": round(len(resolved) / len(rows), 4) if rows else None,
        "graded_resolve_rate": round(len(resolved) / len(graded), 4) if graded else None,
        "patch_rate": round(
            sum(1 for row in executed if row.get("patch_written")) / len(executed), 4
        )
        if executed
        else None,
        "steps": stats("steps"),
        "api_calls": stats("api_calls"),
        "input_tokens": stats("input_tokens"),
        "output_tokens": stats("output_tokens"),
        "agent_elapsed_s": stats("agent_elapsed_s"),
        "tool_calls_total": stats("tool_calls_total"),
        "patch_bytes": stats("patch_bytes"),
        "total_input_tokens": sum(row.get("input_tokens") or 0 for row in executed),
        "total_output_tokens": sum(row.get("output_tokens") or 0 for row in executed),
        "total_prompt_tokens_including_cache": (
            sum(row["prompt_tokens_including_cache"] for row in executed)
            if executed and all("prompt_tokens_including_cache" in row for row in executed)
            else None
        ),
        "total_agent_seconds": round(
            sum(row.get("agent_elapsed_s") or 0 for row in executed), 1
        ),
        "stop_reasons": reasons,
    }


def render_markdown(
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    batch_id: str,
    budget: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"# SWE-bench 批量基线 {batch_id}",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
    ]
    labels = {
        "tasks_planned": "计划题数",
        "tasks_executed": "实际执行",
        "tasks_graded": "官方验收",
        "resolved": "resolved 数",
        "resolve_rate": "解决率",
        "patch_rate": "出补丁率",
        "total_input_tokens": "输入 token 合计",
        "total_output_tokens": "输出 token 合计",
        "total_prompt_tokens_including_cache": "完整输入 token（含缓存）",
        "total_agent_seconds": "Agent 阶段总耗时(s)",
    }
    for key, label in labels.items():
        lines.append(f"| {label} | {summary.get(key)} |")
    for key, label in (
        ("steps", "步数"),
        ("api_calls", "模型调用"),
        ("input_tokens", "输入 token/题"),
        ("output_tokens", "输出 token/题"),
        ("agent_elapsed_s", "耗时/题(s)"),
        ("tool_calls_total", "工具调用/题"),
        ("patch_bytes", "补丁字节"),
    ):
        block = summary.get(key)
        if block:
            lines.append(
                f"| {label}（均值/中位/最小/最大）| "
                f"{block['mean']} / {block['median']} / {block['min']} / {block['max']} |"
            )
    if summary.get("stop_reasons"):
        lines.append(f"| 结束原因分布 | {summary['stop_reasons']} |")
    lines += [
        "",
        "## 每题明细",
        "",
        "| 题目 | 状态 | 原因 | 步数 | 调用 | 输入 | 输出 | 耗时(s) | 工具 | 补丁(B) | 改文件 | resolved | F2P |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        if not row.get("agent_status") and row.get("skip_reason"):
            lines.append(
                f"| {row['instance_id']} | skipped | {row['skip_reason']} | - | - | - | "
                "- | - | - | - | - | - | - |"
            )
            continue
        lines.append(
            "| {iid} | {status} | {reason} | {steps} | {calls} | {inp} | {out} | "
            "{elapsed} | {tools} | {patch} | {files} | {resolved} | {f2p} |".format(
                iid=row["instance_id"],
                status=row.get("agent_status"),
                reason=row.get("reason") or row.get("grading_status") or "",
                steps=row.get("steps", "-"),
                calls=row.get("api_calls", "-"),
                inp=row.get("input_tokens", "-"),
                out=row.get("output_tokens", "-"),
                elapsed=row.get("agent_elapsed_s", "-"),
                tools=row.get("tool_calls_total", "-"),
                patch=row.get("patch_bytes", "-"),
                files=len(row.get("files") or []),
                resolved=row.get("resolved", "未验收"),
                f2p=f"{row.get('f2p_success', 0)}/{row.get('f2p_success', 0) + row.get('f2p_failure', 0)}"
                if row.get("resolved") is not None
                else "-",
            )
        )
    lines += [
        "",
        "## 口径说明",
        "",
        "- 每题独立进程、独立工作区、独立事件流；不加载历史记忆 / RAG / 子 Agent / 自动压缩。",
        "- 模型思考模式**关闭**（`thinking={\"type\": \"disabled\"}`），单次输出上限 "
        f"{(budget or {}).get('max_output_tokens')} tokens。",
        "- 每题一个常驻容器，`/testbed` 保持镜像原样（含 `.git`、编译产物、符号链接），"
        "无 bind mount；模型可用 `python -m pytest` 与 `git diff` 自检。",
        "- 补丁在容器内以「起始快照」为基线导出（`git add -A` + `git diff --cached`），"
        "因此镜像自带的未跟踪产物不会被算成模型改动；空补丁同样计入分母。",
        "- resolved 只认官方 harness 的 report.json，不用进程退出码或模型自述代替。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="SWE-bench Verified 批量基线")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--instances", default="", help="逗号分隔；缺省为全部冻结题目")
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 题（按清单顺序）")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--run-timeout-s", type=int, default=900)
    parser.add_argument("--request-interval-s", type=float, default=0.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--grade", action="store_true")
    parser.add_argument("--thinking-enabled", action="store_true")
    parser.add_argument("--thinking-effort", choices=("low", "high"), default="low")
    parser.add_argument("--repair-policy", choices=("feedback", "converge", "verified"), default="feedback")
    args = parser.parse_args()

    frozen = frozen_instances()
    if args.instances:
        wanted = [x.strip() for x in args.instances.split(",") if x.strip()]
        known = {task["instance_id"] for task in frozen}
        unknown = [x for x in wanted if x not in known]
        if unknown:
            raise SystemExit(f"不在冻结清单内：{unknown}")
        frozen = [task for task in frozen if task["instance_id"] in wanted]
    if args.limit > 0:
        frozen = frozen[: args.limit]

    batch_dir = DATA / "runs" / args.batch_id
    budget = {
        "max_steps": args.max_steps,
        "max_output_tokens": args.max_output_tokens,
        "run_timeout_s": args.run_timeout_s,
        "request_interval_s": args.request_interval_s,
        "thinking_enabled": args.thinking_enabled,
        "thinking_effort": args.thinking_effort,
        "repair_policy": args.repair_policy,
    }

    rows: list[dict[str, Any]] = []
    if args.execute:
        batch_dir.mkdir(parents=True, exist_ok=True)
    for index, task in enumerate(frozen):
        instance_id = str(task["instance_id"])
        image = dataset_image(instance_id)
        if not image_ready(image):
            rows.append({"instance_id": instance_id, "skip_reason": "image_missing"})
            print(f"[skip] {instance_id}: 本地缺镜像", flush=True)
            continue
        run_dir = batch_dir / instance_id
        if not args.execute:
            rows.append({"instance_id": instance_id, "skip_reason": "plan_only"})
            continue
        print(f"[run] {instance_id}", flush=True)
        result = run_one(instance_id, run_dir, budget)
        result["files"] = patch_files(run_dir, instance_id)
        if args.grade:
            print(f"[grade] {instance_id}", flush=True)
            result.update(grade_one(run_dir, instance_id, args.batch_id, "loopsmith-batch"))
        rows.append(result)
        setup_failed = result.get("reason") == "sandbox_initialization_failed"
        if result.get("api_error_kind") or result.get("reason") == "llm_error" or setup_failed:
            rows.extend(
                {"instance_id": str(pending["instance_id"]), "skip_reason": (
                    "blocked_after_setup_error" if setup_failed else "blocked_after_api_error"
                )}
                for pending in frozen[index + 1:]
            )
            print("[stop] 初始化或 API 错误，后续题目不再发送请求", flush=True)
            break

    summary = {
        "batch_id": args.batch_id,
        "kind": "swebench_batch_baseline",
        "budget": budget,
        "llm": {
            "thinking": f"enabled-{args.thinking_effort}" if args.thinking_enabled else "disabled",
            "max_output_tokens_per_call": args.max_output_tokens,
        },
        "model": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "aggregate": aggregate(rows),
        "rows": rows,
    }
    first_config = next(
        (batch_dir / str(task["instance_id"]) / "config.json" for task in frozen), None
    )
    if first_config and first_config.exists():
        config = json.loads(first_config.read_text(encoding="utf-8"))
        summary["model"] = config.get("model")
        summary["provider"] = config.get("provider")
        summary["code_version"] = config.get("code_version")

    if args.execute:
        batch_dir.mkdir(parents=True, exist_ok=True)
        write_json(batch_dir / "summary.json", summary)
        (batch_dir / "summary.md").write_text(
            render_markdown(rows, summary["aggregate"], args.batch_id, budget),
            encoding="utf-8",
            newline="\n",
        )
        fields = [
            "instance_id",
            "agent_status",
            "reason",
            "steps",
            "api_calls",
            "input_tokens",
            "output_tokens",
            "agent_elapsed_s",
            "tool_calls_total",
            "patch_bytes",
            "patch_written",
            "resolved",
            "patch_applied",
            "f2p_success",
            "f2p_failure",
            "p2p_failure",
        ]
        with (batch_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                if row.get("agent_status"):
                    writer.writerow(row)
    print(json.dumps(summary["aggregate"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
