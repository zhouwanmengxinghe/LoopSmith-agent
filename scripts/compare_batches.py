"""两个批次的可比性对照：解决率、成本、工具结构、逐题翻转。

用法：
    python scripts/compare_batches.py --base b3-nothink-24step-full21 \
                                      --cand b4-native-container-24step-full21 \
                                      [--out .eval-data/compare-b3-b4.md]

设计取向：
- 只比较**两边都有 result.json** 的题（交集），避免某一批缺题导致率值不可比。
- 除了率，还要给出翻转明细：哪些题从失败变成功、哪些反向，因为小样本下
  净增 1~2 题可能只是噪声，必须能看见具体是哪几题在动。
- 工具结构按「意图」分类而非按命令文本，同一个 pytest 后面接 | head 不应被
  算成读文件。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".eval-data" / "runs"

# 噪声阈值：21 题二项分布的 1 sigma 约 0.11，取 2 sigma 的题数作为"可分辨"
NOISE_RULE = "21 题样本 ±6pp ≈ 1.3 题；净增 <2 题不足以区分改进与波动"


def classify(command: str) -> str:
    """按意图分类一条 bash 命令，顺序不可换：先判测试，再看管道后的读取。"""
    if re.search(r"pytest|unittest|python -m (?:pytest|unittest)|runtests\.py|tox\b", command):
        return "run_tests"
    if re.search(r"build_ext|pip install|setup\.py|gcc |cython", command):
        return "env_repair"
    if re.search(r"sed -i|\.replace\(|write_text|open\([^)]*['\"]w['\"]", command):
        return "edit"
    if re.search(r"\b(grep|rg|ag)\b", command):
        return "grep"
    if re.search(r"\bfind\b", command):
        return "find"
    if re.search(r"\b(cat|sed -n|head|tail|nl|wc)\b", command):
        return "read_range"
    if re.search(r"\bls\b|\btree\b", command):
        return "list"
    if re.search(r"\bgit\b", command):
        return "git"
    if re.search(r"\bpython\b", command):
        return "python_check"
    return "other"


def load_batch(batch_id: str) -> dict[str, dict]:
    """单题 result.json 只有过程指标，验收字段在批次 summary.json 的 rows 里。

    因此两边都要读：先用 summary.json 建立 instance_id → 验收结果 的映射，
    再把它并进每题的执行指标。summary.json 若还是分批写的旧快照（比 result.json
    少题），以 result.json 为准补上，避免把已在磁盘上的题误判成"没跑"。
    """
    root = DATA / batch_id
    if not root.is_dir():
        raise SystemExit(f"批次不存在: {root}")

    graded: dict[str, dict] = {}
    summary_path = root / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        for row in summary.get("rows") or []:
            if row.get("instance_id") and row.get("resolved") is not None:
                graded[str(row["instance_id"])] = {
                    "resolved": row.get("resolved"),
                    "patch_applied": row.get("patch_applied"),
                    "f2p_success": row.get("f2p_success"),
                    "f2p_failure": row.get("f2p_failure"),
                    "p2p_failure": row.get("p2p_failure"),
                    "grading_status": row.get("grading_status"),
                }

    tasks: dict[str, dict] = {}
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        result_path = run_dir / "result.json"
        if not result_path.exists():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result.update(graded.get(run_dir.name, {}))
        tools: Counter[str] = Counter()
        events = run_dir / "events.jsonl"
        if events.exists():
            for line in events.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") != "tool.call_started":
                    continue
                name = event.get("tool_name")
                params = event.get("params") or {}
                if name == "bash":
                    tools[classify(str(params.get("command") or ""))] += 1
                elif name == "write_file":
                    tools["edit"] += 1
                elif name == "read_file":
                    tools["read_file"] += 1
                elif name == "list_dir":
                    tools["list"] += 1
                else:
                    tools["other"] += 1
        result["_tools"] = tools
        tasks[run_dir.name] = result
    return tasks


def stat(values: list[float]) -> tuple[float, float] | None:
    values = [v for v in values if isinstance(v, (int, float))]
    if not values:
        return None
    return statistics.fmean(values), statistics.median(values)


def rate_rows(base: dict[str, dict], cand: dict[str, dict]) -> list[dict]:
    rows = []
    for instance_id in sorted(set(base) & set(cand)):
        b, c = base[instance_id], cand[instance_id]
        if b.get("resolved") is None or c.get("resolved") is None:
            continue
        rows.append(
            {
                "id": instance_id,
                "b_resolved": bool(b.get("resolved")),
                "c_resolved": bool(c.get("resolved")),
                "b_steps": b.get("steps"),
                "c_steps": c.get("steps"),
                "b_patch": b.get("patch_bytes"),
                "c_patch": c.get("patch_bytes"),
                "b_f2p": (b.get("f2p_success") or 0, (b.get("f2p_success") or 0) + (b.get("f2p_failure") or 0)),
                "c_f2p": (c.get("f2p_success") or 0, (c.get("f2p_success") or 0) + (c.get("f2p_failure") or 0)),
                "b_p2p_fail": b.get("p2p_failure"),
                "c_p2p_fail": c.get("p2p_failure"),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="批次 A/B 对照")
    parser.add_argument("--base", required=True)
    parser.add_argument("--cand", required=True)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    base, cand = load_batch(args.base), load_batch(args.cand)
    rows = rate_rows(base, cand)
    if not rows:
        raise SystemExit("两个批次没有共同的可验收题目")

    b_ok = sum(r["b_resolved"] for r in rows)
    c_ok = sum(r["c_resolved"] for r in rows)
    n = len(rows)

    lines = [
        f"# 批次对照：{args.base} → {args.cand}",
        "",
        f"共同题目 {n} 道（只统计两边都有 result.json 且已验收的题）。",
        "",
        "## 汇总",
        "",
        "| 指标 | base | cand | 变化 |",
        "| --- | --- | --- | --- |",
        f"| resolved | {b_ok}/{n} ({b_ok / n:.1%}) | {c_ok}/{n} ({c_ok / n:.1%}) | "
        f"{c_ok - b_ok:+d} 题 / {(c_ok - b_ok) / n * 100:+.1f}pp |",
    ]

    for key, label in (
        ("steps", "步数"),
        ("tool_calls_total", "工具调用"),
        ("input_tokens", "输入 token"),
        ("output_tokens", "输出 token"),
        ("agent_elapsed_s", "耗时(s)"),
        ("patch_bytes", "补丁字节"),
    ):
        b_stat = stat([base[r["id"]].get(key) for r in rows])
        c_stat = stat([cand[r["id"]].get(key) for r in rows])
        if not b_stat or not c_stat:
            continue
        delta = c_stat[0] - b_stat[0]
        pct = (delta / b_stat[0] * 100) if b_stat[0] else 0.0
        lines.append(
            f"| {label}（均值/中位） | {b_stat[0]:.0f} / {b_stat[1]:.0f} | "
            f"{c_stat[0]:.0f} / {c_stat[1]:.0f} | {delta:+.0f} ({pct:+.0f}%) |"
        )

    lines += ["", "## 逐题翻转", "", "| 题目 | base | cand | 步数 | 补丁(B) | F2P |", "| --- | --- | --- | --- | --- | --- |"]
    flips = []
    for r in rows:
        if r["b_resolved"] == r["c_resolved"]:
            continue
        mark = "✅ 新增" if r["c_resolved"] else "❌ 丢失"
        flips.append(f"{r['id']} {mark}")
        lines.append(
            f"| {r['id']} | {'resolved' if r['b_resolved'] else 'fail'} | "
            f"{'resolved' if r['c_resolved'] else 'fail'} | "
            f"{r['b_steps']}→{r['c_steps']} | {r['b_patch']}→{r['c_patch']} | "
            f"{r['b_f2p'][0]}/{r['b_f2p'][1]} → {r['c_f2p'][0]}/{r['c_f2p'][1]} |"
        )
    if not flips:
        lines.append("| （无翻转） | - | - | - | - | - |")

    lines += [
        "",
        f"净变化 **{c_ok - b_ok:+d} 题**。{NOISE_RULE}。",
        "",
        "## 工具结构对比（所有已跑题目合计）",
        "",
        "| 类别 | base | cand | 变化 |",
        "| --- | --- | --- | --- |",
    ]
    b_tools: Counter[str] = Counter()
    c_tools: Counter[str] = Counter()
    for tasks, bucket in ((base, b_tools), (cand, c_tools)):
        for result in tasks.values():
            bucket.update(result["_tools"])
    b_total = sum(b_tools.values()) or 1
    c_total = sum(c_tools.values()) or 1
    for key in sorted(set(b_tools) | set(c_tools), key=lambda k: -(b_tools[k] + c_tools[k])):
        bv, cv = b_tools[key], c_tools[key]
        lines.append(
            f"| {key} | {bv} ({bv / b_total:.1%}) | {cv} ({cv / c_total:.1%}) | "
            f"{cv - bv:+d} ({(cv / c_total - bv / b_total) * 100:+.1f}pp) |"
        )

    lines += ["", "## 分类口径", "", f"- 分类函数见 `scripts/{Path(__file__).name}` 的 `classify()`，按意图优先于管道后缀。", ""]

    text = "\n".join(lines)
    print(text)
    if args.out:
        target = Path(args.out)
        if not target.is_absolute():
            target = ROOT / target
        target.write_text(text + "\n", encoding="utf-8", newline="\n")
        print(f"\n[written] {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
