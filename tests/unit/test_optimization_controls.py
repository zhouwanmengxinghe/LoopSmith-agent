from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from loopsmith.core.context import ExecutionContext
from loopsmith.core.events.bus import EventBus
from loopsmith.core.llm.provider import AnthropicProvider
from loopsmith.core.llm.types import LlmResponse
from loopsmith.core.loop import AgentLoop
from loopsmith.core.tools.registry import ToolRegistry
from loopsmith.evaluation.feedback import RunTestsTool
from loopsmith.evaluation.feedback import test_feedback as parse_feedback
from loopsmith.evaluation.token_budget import TokenBudget

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "optimization_runner", ROOT / "scripts/run_swe_baseline.py"
)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


# 功能：验证管道零退出码、空测试、源码文本和真实摘要被正确区分
# 设计：覆盖此前轨迹中的误报，不访问 Docker 或模型
@pytest.mark.parametrize(
    ("rc", "text", "status"),
    [
        (0, "2 failed, 167 passed in 24.62s", "failed"),
        (0, "6 passed in 1.0s", "passed"),
        (0, "from _pytest import runner\n# pytest tests", "unknown"),
        (5, "no tests ran in 0.1s", "no_tests"),
        (0, "3 skipped in 0.1s", "no_tests"),
        (2, "1 error in 0.4s", "collection_or_runner_error"),
        (124, "", "timeout"),
        (0, "Ran 4 tests in 1.2s\n\nOK", "passed"),
        (1, "Ran 4 tests in 1.2s\nFAILED (failures=1)", "failed"),
        (0, "Ran 4 tests in 1.2s\nOK (skipped=4)", "no_tests"),
    ],
)
def test_feedback_status(rc: int, text: str, status: str) -> None:
    assert parse_feedback(rc, text)["status"] == status


# 功能：验证读测试源码不计入验证，真实失败不能被管道零状态掩盖
# 设计：直接复现守卫的两个已知错误输入，检查实际状态而非实现细节
def test_guard_uses_test_evidence() -> None:
    guard = runner.FinishGuard()
    guard.note_command("cat src/_pytest/runner.py", 0, "import pytest")
    assert guard.verify_runs == 0
    guard.note_command("pytest tests | tail -3", 0, "2 failed, 167 passed in 1.2s")
    assert guard.verify_runs == 1
    assert guard.last_verify_rc == 1
    guard.note_command("pytest tests", 2, "1 error in 0.4s")
    assert guard.verify_runs == 1


# 功能：验证未返回 usage 的失败请求仍占预算，不能被清零后反复重试
# 设计：使用小账本触发拒绝，结算一笔后检查剩余额度变化
def test_token_budget_keeps_unreported_reservations(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    budget = TokenBudget(path, limit=20_000)
    first = budget.reserve({"messages": []}, 100)
    budget.reserve({"messages": []}, 100)
    with pytest.raises(RuntimeError, match="exhausted"):
        budget.reserve({"messages": []}, 100)
    budget.settle(first, 1200)
    state = json.loads(path.read_text())
    assert state["reported_tokens"] == 1200
    assert state["reserved_tokens"] > 8000
    assert state["calls_with_usage"] == 1
    assert not path.with_suffix(".lock").exists()


class FakeContainer:
    # 返回预设完整测试摘要，确保工具使用可信命令且保存真实退出状态
    async def exec(self, command: str) -> tuple[int, str]:
        assert "set -o pipefail" in command and "pytest tests" in command
        assert "tail -c" in command
        return 0, "2 failed, 7 passed in 1.0s\n__LOOPSMITH_TEST_RC__=1\n"


# 功能：验证专用测试工具即使外层退出零也反馈失败且记录真实测试次数
# 设计：模拟旧管道错误边界，检查结构化输出及守卫最终状态
async def test_run_tests_preserves_failure() -> None:
    guard = runner.FinishGuard()
    result = await RunTestsTool(FakeContainer(), "pytest tests", guard).invoke({})
    assert result.is_error
    assert json.loads(result.content.splitlines()[0])["status"] == "failed"
    assert guard.last_verify_rc == 1 and guard.verify_runs == 1


# 功能：验证评测可以关闭 SDK 隐式重试，避免未知消耗重复计费
# 设计：截获 SDK 构造参数，不建立网络连接或读取真实凭据
def test_sdk_retries_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    constructor = MagicMock()
    monkeypatch.setattr("loopsmith.core.llm.provider.anthropic.AsyncAnthropic", constructor)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-placeholder")
    AnthropicProvider("deepseek-v4-flash", sdk_max_retries=0)
    assert constructor.call_args.kwargs["max_retries"] == 0


class EndlessProvider:
    # 持续返回未完成响应，检验守卫提醒不能扩大原定调用上限
    def __init__(self) -> None:
        self.calls = 0

    # 返回模拟响应而不访问网络
    async def chat(self, **kwargs: object) -> LlmResponse:
        self.calls += 1
        return LlmResponse(text="still working", stop_reason="max_tokens")


# 功能：验证中途和收尾提醒真正进入循环且不会突破 24 次调用预算
# 设计：用始终未完成的模型模拟最差情况，检查循环终止和提示注入而非只调用守卫方法
async def test_convergence_reminders_stay_within_budget() -> None:
    provider = EndlessProvider()
    guard = runner.ConvergenceGuard()
    context = ExecutionContext("unit", "repair", 24)
    await AgentLoop(provider, ToolRegistry(), EventBus(), finish_guard=guard).run(context)
    assert provider.calls == 24
    assert context.reason == "exceeded_max_steps"
    assert guard.phase_reminders == ["localization", "verification"]
    texts = [str(message["content"]) for message in context.messages if message["role"] == "user"]
    assert any("Only 4 calls remain" in text for text in texts)


# 功能：验证明显越界的缓存和全盘搜索在执行前被拒绝，正常源码搜索仍可执行
# 设计：使用不可调用的假容器确保拒绝路径没有产生实际进程或额外模型调用
@pytest.mark.parametrize(
    "command",
    ["find / -name json.py", "pip cache list", "grep -rl X ~/.cache/pip", "git show HEAD"],
)
async def test_external_search_is_blocked(command: str) -> None:
    tool = runner.ContainerBashTool(None, restrict_search=True)
    result = await tool.invoke({"command": command})
    assert result.is_error and result.error_type == "scope_error"


class SourceSearchContainer:
    # 模拟合法仓库内查找，确认过滤器不会封禁所有普通 shell 操作
    async def exec(self, command: str, **kwargs: object) -> tuple[int, str]:
        assert command == "cd /testbed && grep -n X module.py"
        return 0, "10:X"


# 功能：验证源码范围内的正常定位命令仍能执行
# 设计：与越界拒绝用例配对，避免用一律拒绝的实现获得表面通过
async def test_source_search_remains_available() -> None:
    result = await runner.ContainerBashTool(SourceSearchContainer(), restrict_search=True).invoke(
        {"command": "cd /testbed && grep -n X module.py"}
    )
    assert not result.is_error and result.content == "10:X"


# 功能：验证新的收集错误不会被更早的通过状态掩盖
# 设计：按通过后收集失败的真实顺序喂给守卫，检查结束时仍要求重新验证
async def test_latest_invalid_test_invalidates_finish() -> None:
    guard = runner.FinishGuard(test_command="pytest tests")
    guard.note_test(parse_feedback(0, "4 passed in 1.0s"))
    guard.note_test(parse_feedback(2, "1 error in 1.0s"))
    reminder = await guard.before_finish(ExecutionContext("unit", "repair", 24))
    assert reminder and "collection_or_runner_error" in reminder


# 功能：验证停止、环境错误和未执行题目仍保留在修复率分母内
# 设计：混合成功、缺失验收与未执行三种结果，避免只对已验收成功样本计算高分
def test_batch_rate_keeps_planned_denominator() -> None:
    spec = importlib.util.spec_from_file_location(
        "optimization_batch", ROOT / "scripts/run_swe_batch.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metrics = module.aggregate(
        [
            {"agent_status": "success", "resolved": True},
            {"agent_status": "failed", "resolved": None},
            {"skip_reason": "blocked_after_api_error"},
        ]
    )
    assert metrics["tasks_planned"] == 3 and metrics["tasks_graded"] == 1
    assert metrics["resolve_rate"] == 0.3333
    assert metrics["graded_resolve_rate"] == 1.0


# 功能：验证初始化失败仍有结果和明确原因，且不会创建模型客户端
# 设计：隔离临时目录、假配置与假容器，复现 Git 树校验失败而不使用真实密钥或 Docker
async def test_setup_failure_is_recorded_without_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    containers = []

    class FailedContainer:
        # 模拟一次性容器，不创建真实进程
        def __init__(self, *args: object) -> None:
            self.started = False
            self.stopped = False
            containers.append(self)

        # 模拟启动成功，让后续源码校验产生预期失败
        def start(self) -> None:
            self.started = True

        # 记录清理动作供测试检查
        def stop(self) -> None:
            self.stopped = True

    # 在模型创建之前注入可复现的初始化失败
    async def fail_alignment(*args: object, **kwargs: object) -> dict[str, object]:
        raise SystemExit("source tree validation failed")

    # 初始化失败后若仍尝试创建模型，则直接使测试失败
    def forbid_provider(*args: object, **kwargs: object) -> None:
        raise AssertionError("Model client must not be created")

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "load_instance",
        lambda *args: {
            "base_commit": "a" * 40,
            "repo": "unit/repo",
            "problem_statement": "unit problem",
        },
    )
    monkeypatch.setattr(runner, "instance_image", lambda *args: "unit-image")
    monkeypatch.setattr(runner, "image_present", lambda *args: True)
    monkeypatch.setattr(runner, "extract_test_command", lambda *args: "pytest tests")
    monkeypatch.setattr(runner, "code_version", lambda: {})
    monkeypatch.setattr(runner, "TaskContainer", FailedContainer)
    monkeypatch.setattr(runner, "align_to_base", fail_alignment)
    monkeypatch.setattr(runner, "create_provider", forbid_provider)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-placeholder")
    monkeypatch.setattr(
        "loopsmith.core.config.get_config",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(
                provider="anthropic",
                default_model="deepseek-unit",
                base_url="https://api.deepseek.com/anthropic",
            )
        ),
    )
    output = tmp_path / ".eval-data" / "setup-failure"
    args = SimpleNamespace(
        instance_id="unit",
        output=output,
        execute=True,
        bash_image=None,
        max_steps=24,
        max_output_tokens=4096,
        run_timeout_s=900,
        request_interval_s=0,
        repair_policy="converge",
        thinking_enabled=False,
        thinking_effort="low",
    )
    await runner.run(args)
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["reason"] == "sandbox_initialization_failed"
    assert result["api_calls"] == 0 and not result["patch_written"]
    assert "source tree validation failed" in result["setup_error"]
    assert containers[0].stopped


# 功能：验证首题初始化失败后不会调用后续任务，全部计划题目仍记入分母
# 设计：模拟三个镜像均存在但首题失败，直接运行批次入口检查调度和保存的汇总
def test_batch_stops_after_setup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "setup_stop_batch", ROOT / "scripts/run_swe_batch.py"
    )
    assert spec and spec.loader
    batch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batch)
    called = []

    # 模拟首题在模型调用前失败，不产生真实模型请求
    def fail_first(instance: str, *args: object) -> dict[str, object]:
        called.append(instance)
        return {
            "instance_id": instance,
            "agent_status": "failed",
            "reason": "sandbox_initialization_failed",
            "api_calls": 0,
        }

    monkeypatch.setattr(batch, "DATA", tmp_path)
    monkeypatch.setattr(
        batch, "frozen_instances", lambda: [{"instance_id": x} for x in ("a", "b", "c")]
    )
    monkeypatch.setattr(batch, "dataset_image", lambda instance: "unit-image")
    monkeypatch.setattr(batch, "image_ready", lambda image: True)
    monkeypatch.setattr(batch, "run_one", fail_first)
    monkeypatch.setattr(batch, "patch_files", lambda *args: [])
    monkeypatch.setattr(sys, "argv", ["run_swe_batch.py", "--batch-id", "unit", "--execute"])
    batch.main()
    result = json.loads((tmp_path / "runs/unit/summary.json").read_text(encoding="utf-8"))
    assert called == ["a"]
    assert result["aggregate"]["tasks_planned"] == 3
    assert [row.get("skip_reason") for row in result["rows"][1:]] == [
        "blocked_after_setup_error"
    ] * 2
