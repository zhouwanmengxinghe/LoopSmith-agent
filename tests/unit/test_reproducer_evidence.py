from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from loopsmith.core.context import ExecutionContext
from loopsmith.core.events.bus import EventBus
from loopsmith.core.llm.types import LlmResponse
from loopsmith.core.loop import AgentLoop
from loopsmith.core.tools.registry import ToolRegistry
from loopsmith.evaluation.feedback import RunTestsTool
from loopsmith.evaluation.reproducer import IssueReproducerTool

spec = importlib.util.spec_from_file_location(
    "verified_runner", Path(__file__).resolve().parents[2] / "scripts/run_swe_baseline.py"
)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ReproducerContainer:
    # 模拟同一脚本基线失败、修复后通过，并记录实际写入字节供断言
    def __init__(self, results: list[tuple[int, str]]) -> None:
        self.results = iter(results)
        self.scripts: list[bytes] = []

    # 记录不可变脚本，每次返回预设的真实执行边界结果
    async def exec_bytes(self, command: str, *, stdin: bytes, **kwargs: object) -> tuple[int, str]:
        assert "loopsmith-issue-reproducer.py" in command
        self.scripts.append(stdin)
        return next(self.results)


# 功能：验证同一断言脚本从失败到通过且编辑后旧证据失效
# 设计：模拟真实工具调用序列，并检查脚本字节相同和最终内容指纹一致性
async def test_reproducer_is_immutable_and_bound_to_candidate(tmp_path: Path) -> None:
    state = SimpleNamespace(value="base")

    # 返回当前模拟候选的内容标识
    async def fingerprint() -> str:
        return state.value

    container = ReproducerContainer([(1, "AssertionError: bug"), (0, "passed")])
    tool = IssueReproducerTool(container, "base", fingerprint, tmp_path)
    assert not (await tool.invoke({"script": "assert value == 1"})).is_error
    assert tool.baseline_returncode == 1
    conflict = await tool.invoke({"script": "assert True"})
    assert conflict.is_error and len(container.scripts) == 1
    state.value = "patched"
    assert not (await tool.invoke({})).is_error
    assert container.scripts[0] == container.scripts[1]
    guard = runner.VerifiedRepairGuard()
    guard.issue, guard.fingerprint = tool, fingerprint
    guard.note_test(
        {"valid_test_run": True, "status": "passed", "candidate_fingerprint": "patched"}
    )
    assert (await guard.evidence())[0]
    state.value = "edited-again"
    assert not (await guard.evidence())[0]


# 功能：验证绿色基线或导入失败不能被当作成功复现问题
# 设计：分别提供两种常见假证据，检查脚本未固定且没有可用通过指纹
@pytest.mark.parametrize("result", [(0, "all green"), (1, "ModuleNotFoundError: fixture")])
async def test_invalid_baseline_is_rejected(tmp_path: Path, result: tuple[int, str]) -> None:
    # 返回未编辑的基线标识
    async def fingerprint() -> str:
        return "base"

    tool = IssueReproducerTool(ReproducerContainer([result]), "base", fingerprint, tmp_path)
    assert (await tool.invoke({"script": "assert value == 1"})).is_error
    assert tool.script is None and tool.verified_fingerprint is None


# 功能：验证对旧失败的解释只能和已通过的问题断言一起记录，不会把失败改成通过
# 设计：先验证缺解释被拒绝，再记录解释，最后令问题断言失败确认解释不能绕过门槛
async def test_explained_regressions_do_not_override_issue_failure(tmp_path: Path) -> None:
    state = SimpleNamespace(value="base")

    # 返回当前模拟候选的内容标识
    async def fingerprint() -> str:
        return state.value

    tool = IssueReproducerTool(
        ReproducerContainer([(1, "AssertionError"), (0, "ok"), (0, "ok"), (1, "AssertionError")]),
        "base",
        fingerprint,
        tmp_path,
    )
    await tool.invoke({"script": "assert value == 1"})
    state.value = "patched"
    await tool.invoke({})
    guard = runner.VerifiedRepairGuard()
    guard.issue, guard.fingerprint = tool, fingerprint
    guard.note_test(
        {"valid_test_run": True, "status": "failed", "candidate_fingerprint": "patched"}
    )
    assert not (await guard.evidence())[0]
    await tool.invoke(
        {"explain_test_failures": "The issue explicitly changes this display format."}
    )
    assert (await guard.evidence())[0]
    assert guard.verification_status == "issue_passed_regression_failures_explained_by_model"
    await tool.invoke({"explain_test_failures": "Still expected."})
    assert not (await guard.evidence())[0]


class FinalAnswerProvider:
    # 记录模拟模型调用次数，验证审查不会额外发请求
    def __init__(self) -> None:
        self.calls = 0

    # 模拟没有证据就不断宣称完成的模型
    async def chat(self, **kwargs: object) -> LlmResponse:
        self.calls += 1
        return LlmResponse(text="done", stop_reason="end_turn")


# 功能：验证最后一步结束和守卫提醒耗尽都不能把未验证任务标成成功
# 设计：直接跑真实循环，分别覆盖 1 步上限和有余量的连续结束响应
@pytest.mark.parametrize(("steps", "calls"), [(1, 1), (5, 3)])
async def test_unverified_end_turn_is_failed(tmp_path: Path, steps: int, calls: int) -> None:
    # 返回未编辑的基线标识
    async def fingerprint() -> str:
        return "base"

    guard = runner.VerifiedRepairGuard()
    guard.fingerprint = fingerprint
    guard.issue = IssueReproducerTool(ReproducerContainer([]), "base", fingerprint, tmp_path)
    context = ExecutionContext("unit", "repair", steps)
    provider = FinalAnswerProvider()
    await AgentLoop(provider, ToolRegistry(), EventBus(), finish_guard=guard).run(context)
    assert context.status == "failed" and context.reason == "verification_incomplete"
    assert provider.calls == calls


# 功能：验证测试过程中代码变化会使回归证据失效
# 设计：模拟开始和结束指纹不一致，即使输出显示通过也不能覆盖新候选
async def test_test_run_does_not_cover_changed_candidate() -> None:
    hashes = iter(["before", "after"])
    recorded = []

    # 模拟测试前后候选发生变化
    async def fingerprint() -> str:
        return next(hashes)

    class Container:
        # 返回真实形式的通过摘要，但候选在测试期间变化
        async def exec(self, command: str) -> tuple[int, str]:
            return 0, "1 passed in 0.1s\n__LOOPSMITH_TEST_RC__=0\n"

    guard = SimpleNamespace(fingerprint=fingerprint, note_test=recorded.append)
    result = await RunTestsTool(Container(), "pytest tests", guard).invoke({})
    assert result.is_error
    assert recorded[0]["status"] == "candidate_changed_during_tests"
    assert not recorded[0]["valid_test_run"]


# 功能：验证真实源码已编辑后不会因 shell 正则漏报而收到无编辑提示
# 设计：指纹与基线不同但旧编辑计数为零，模拟 Python 脚本直接修改文件的路径
async def test_localization_uses_content_instead_of_shell_regex() -> None:
    # 返回已经编辑的候选指纹
    async def fingerprint() -> str:
        return "patched"

    guard = runner.VerifiedRepairGuard()
    guard.fingerprint = fingerprint
    guard.issue = SimpleNamespace(initial_fingerprint="base")
    context = ExecutionContext("unit", "repair", 24)
    context.step = 12
    assert await guard.before_step(context) is None
    context.step = 21
    assert "Only 4 calls remain" in (await guard.before_step(context) or "")
