from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from loopsmith.core.bus.events import StepFinishedEvent, StepStartedEvent
from loopsmith.core.context import ExecutionContext
from loopsmith.core.events.bus import EventBus
from loopsmith.core.llm.base import LLMProvider
from loopsmith.core.tools.invocation import invoke_tool
from loopsmith.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from loopsmith.core.compact.compactor import Compactor
    from loopsmith.core.permissions.manager import PermissionManager

    class FinishGuard(Protocol):
        """收尾守卫接口：返回文本则不结束本轮，把文本作为提醒注入上下文。"""

        async def before_finish(self, context: ExecutionContext) -> str | None: ...


log = logging.getLogger(__name__)

def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 初始化循环依赖：LLM provider、工具注册表、事件总线，以及可选的权限管理器、
    # 压缩器和 session ID
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
        finish_guard: FinishGuard | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id
        self._finish_guard = finish_guard

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # 可选评测策略在预算内提醒收敛，不增加模型调用次数
            step_hook = getattr(self._finish_guard, "before_step", None)
            if step_hook is not None:
                reminder = await step_hook(context)
                if reminder:
                    context.add_user_message(reminder)

            # [plan] call LLM — API errors terminate the run
            try:
                response = await self._provider.chat(
                    messages=context.messages,
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=context.system_prompt(
                        "You are a helpful AI assistant. "
                        "Use the available tools to complete the user's goal. "
                        "When the goal is fully achieved, respond with a final answer "
                        "and do not call any more tools."
                    ),
                )
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            context.add_assistant_message(blocks)

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                for tc in response.tool_calls:
                    result = await invoke_tool(
                        self._registry, tc, self._bus, context.run_id,
                        permission_manager=self._permission_manager,
                        session_id=self._session_id,
                    )
                    context.add_tool_result(tc.id, result.content, is_error=result.is_error)
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for tc in response.tool_calls:
                    context.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call could "
                        "be completed. "
                        "Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            # Termination check — end_turn wins over max_steps if both hit on same step
            if response.stop_reason == "end_turn":
                # 收尾守卫：编辑后未验证等问题在这里被拦下，注入提醒继续跑。
                # 只在还有步数预算时生效，避免把"撞上限"变成"多一次空转"。
                nudge: str | None = None
                if self._finish_guard is not None and context.step < context.max_steps:
                    nudge = await self._finish_guard.before_finish(context)
                if nudge:
                    context.add_user_message(nudge)
                else:
                    context.result = response.text or ""
                    if not context.is_done():
                        context.mark_success()
            elif context.step >= context.max_steps:
                context.mark_failed("exceeded_max_steps")

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._compactor.compact(context, self._provider)

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

        # 可选证据审查不发模型请求，避免最后一步结束绕过验证状态记录
        after_run = getattr(self._finish_guard, "after_run", None)
        if after_run is not None:
            await after_run(context)
