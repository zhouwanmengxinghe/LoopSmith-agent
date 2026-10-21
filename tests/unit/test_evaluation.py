from __future__ import annotations

import json
from pathlib import Path

import pytest

from loopsmith.evaluation.data import QUIX_TASKS, prepare, select_swe, verify, write_json
from loopsmith.evaluation.grading import parse_junit
from loopsmith.evaluation.grading import test_environment as grading_environment
from loopsmith.evaluation.swe import grade_swe


# 功能：验证固定种子选题可重复，仓库分布轮转且不会重复抽题
# 设计：打乱输入顺序后应获得同一清单，排除上游返回顺序影响
def test_selection_reproducible() -> None:
    rows = [
        {"instance_id": f"{repo}-{index}", "repo": repo}
        for repo in ("a", "b", "c")
        for index in range(5)
    ]
    selected = select_swe(rows, 9, 20261018)
    assert selected == select_swe(list(reversed(rows)), 9, 20261018)
    assert len({item["instance_id"] for item in selected}) == 9
    assert [item["repo"] for item in selected[:3]] == ["a", "b", "c"]
    with pytest.raises(ValueError):
        select_swe(rows, 16, 1)


# 功能：验证独立验收拒绝零用例、跳过、收集错误和退出码异常
# 设计：使用最小真实 JUnit XML，避免只依赖 pytest 退出码
@pytest.mark.parametrize(
    ("cases", "code", "status"),
    [
        ('<testcase name="a"/>', 0, "passed"),
        ("", 0, "test_error"),
        ('<testcase name="a"><skipped/></testcase>', 0, "test_error"),
        ('<testcase name="a"><failure/></testcase>', 1, "failed"),
        ('<testcase name="a"><error/></testcase>', 2, "test_error"),
        ('<testcase name="a"/>', 1, "test_error"),
    ],
)
def test_junit_fail_closed(tmp_path: Path, cases: str, code: int, status: str) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    assert parse_junit(report, code)["status"] == status


# 功能：验证候选测试进程不会继承 API 密钥
# 设计：注入多个常见密钥变量并检查实际子进程环境白名单
def test_grading_environment_strips_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HKU_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    env = grading_environment(tmp_path)
    assert "secret" not in json.dumps(env)
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


# 创建最小基准源数据，验证冻结和答案隔离而无需外网
def fake_benchmark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "data"
    for folder in (
        "python_programs",
        "correct_python_programs",
        "python_testcases",
        "json_testcases",
    ):
        (root / "quixbugs" / folder).mkdir(parents=True)
    (root / "quixbugs" / "conftest.py").write_text("# trusted tests")
    for name in QUIX_TASKS:
        (root / "quixbugs" / "python_programs" / f"{name}.py").write_text("# buggy")
    write_json(
        root / "swe-bench-verified.json",
        [
            {
                "instance_id": "repo-1",
                "repo": "owner/repo",
                "base_commit": "a" * 40,
                "problem_statement": "repair a defect",
                "patch": "SECRET_GOLD_PATCH",
                "test_patch": "SECRET_TEST",
            }
        ],
    )
    monkeypatch.setattr(
        "loopsmith.evaluation.data.subprocess.check_output", lambda *a, **kw: "b" * 40
    )
    prepared = tmp_path / "prepared"
    prepare(root, prepared, count=1)
    return root, prepared


# 功能：验证任务不含答案，且验收测试被修改时冻结校验会拒绝
# 设计：用明确的答案标记检查真实落盘产物，再篡改文件验证防护
def test_frozen_data_no_answer_leak_and_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, prepared = fake_benchmark(tmp_path, monkeypatch)
    for path in (prepared / "agent").rglob("*.json"):
        assert "SECRET_" not in path.read_text(encoding="utf-8")
    verify(root, prepared)
    (root / "quixbugs" / "conftest.py").write_text("# changed")
    with pytest.raises(ValueError, match="Protected"):
        verify(root, prepared)


# 功能：验证 SWE 计划使用冻结数据且默认不启动容器
# 设计：将 Docker 检查替换为不可用，仍应成功生成参考和负对照计划
def test_swe_preflight_plan_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, prepared = fake_benchmark(tmp_path, monkeypatch)
    monkeypatch.setattr("loopsmith.evaluation.swe.doctor", lambda: {"ready": False})
    for mode in ("reference", "negative"):
        reports = tmp_path / mode
        result = grade_swe(root, prepared, reports, mode=mode)
        assert result["status"] == "prepared_not_executed"
        assert result["kind"] == "environment_preflight"
        assert str(reports / "grader-only-dataset.json") in result["command"]
        entries = (reports / "predictions.jsonl").read_text()
        assert ("SECRET_GOLD_PATCH" in entries) == (mode == "reference")


# 功能：验证候选结果缺题时不可进入 SWE 正式验收
# 设计：用少预测模拟缺题，确认不能通过缩小分母提高成绩
def test_swe_rejects_missing_predictions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, prepared = fake_benchmark(tmp_path, monkeypatch)
    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text("")
    with pytest.raises(ValueError, match="exactly"):
        grade_swe(root, prepared, tmp_path / "report", mode="candidate", predictions=predictions)
