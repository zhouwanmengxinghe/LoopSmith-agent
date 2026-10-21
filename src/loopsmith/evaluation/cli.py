from __future__ import annotations

import argparse
import json
from pathlib import Path

from loopsmith.evaluation.data import fetch_swe, inventory, prepare, verify
from loopsmith.evaluation.grading import export_predictions, grade_quix, preflight_quix
from loopsmith.evaluation.swe import doctor, grade_swe


# 提供默认不访问模型的本地准备与验收命令
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline benchmark preparation and independent grading"
    )
    parser.add_argument("--data", type=Path, default=Path(".eval-data"))
    parser.add_argument("--prepared", type=Path, default=Path(".eval-data/frozen-v1"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch-swe", help="Download public metadata only; proxy required")
    setup = sub.add_parser("prepare")
    setup.add_argument("--count", type=int, default=30)
    setup.add_argument("--seed", type=int, default=20261018)
    sub.add_parser("verify")
    sub.add_parser("doctor")
    export_inventory = sub.add_parser("inventory")
    export_inventory.add_argument("--output", type=Path, default=Path("benchmarks/selection.json"))
    swe = sub.add_parser("grade-swe")
    swe.add_argument("--mode", choices=("reference", "negative", "candidate"), required=True)
    swe.add_argument("--reports", type=Path, required=True)
    swe.add_argument("--predictions", type=Path)
    swe.add_argument("--instance-id")
    swe.add_argument("--execute", action="store_true", help="Start official Docker grading; no LLM")
    # 官方 harness 的 --timeout 是"整题测试套件"上限，默认 300s。慢套件（如
    # requests-2317）会被判 tests_timed_out 且 outcomes 为空，看起来像装置故障，
    # 实际只是没跑完。单独验收时允许调大。
    swe.add_argument("--harness-timeout", type=int, default=300)
    preflight = sub.add_parser("preflight-quix")
    preflight.add_argument("--reports", type=Path, required=True)
    preflight.add_argument("--timeout", type=int, default=30)
    preflight.add_argument("--resume", action="store_true")
    grade = sub.add_parser("grade-quix")
    grade.add_argument("--program", required=True)
    grade.add_argument("--candidate", type=Path, required=True)
    grade.add_argument("--reports", type=Path, required=True)
    grade.add_argument("--timeout", type=int, default=30)
    export = sub.add_parser("export-swe")
    export.add_argument("--patches", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--model-name", default="loopsmith-hku")
    args = parser.parse_args()
    root, output = args.data.resolve(), args.prepared.resolve()
    if args.command == "fetch-swe":
        print(fetch_swe(root))
    elif args.command == "prepare":
        manifest = prepare(root, output, count=args.count, seed=args.seed)
        print(
            json.dumps(
                {
                    "quixbugs": len(manifest["quixbugs"]),
                    "swe_bench": len(manifest["swe_bench"]),
                    "status": manifest["status"],
                }
            )
        )
    elif args.command == "verify":
        manifest = verify(root, output)
        print(f"Frozen inputs verified; SWE environment status: {manifest['status']}")
    elif args.command == "preflight-quix":
        if args.timeout <= 0:
            parser.error("timeout must be positive")
        summary = preflight_quix(root, output, args.reports.resolve(), args.timeout,
                                 resume=args.resume)
        print(f"Eligible: {summary['eligible']}/{summary['total']} (not an Agent score)")
        if summary["eligible"] != summary["total"]:
            raise SystemExit(1)
    elif args.command == "grade-quix":
        if args.timeout <= 0:
            parser.error("timeout must be positive")
        result = grade_quix(
            root,
            output,
            args.program,
            args.candidate.resolve(),
            args.reports.resolve(),
            timeout=args.timeout,
        )
        print(json.dumps(result))
        if result["status"] != "passed":
            raise SystemExit(1)
    elif args.command == "doctor":
        print(json.dumps(doctor()))
    elif args.command == "inventory":
        result = inventory(root, output, args.output)
        print(f"Exported {len(result['swe_bench'])} fixed SWE task IDs")
    elif args.command == "grade-swe":
        result = grade_swe(
            root,
            output,
            args.reports.resolve(),
            mode=args.mode,
            predictions=args.predictions,
            instance_id=args.instance_id,
            execute=args.execute,
            harness_timeout_s=args.harness_timeout,
        )
        print(json.dumps(result, ensure_ascii=False))
        if result["status"] in ("environment_or_harness_error", "preflight_failed"):
            raise SystemExit(1)
    else:
        count = export_predictions(root, output, args.patches, args.output, args.model_name)
        print(f"Exported {count} predictions; use the official Docker harness for SWE grading")


if __name__ == "__main__":
    main()
