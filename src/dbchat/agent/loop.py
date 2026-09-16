"""唯一的 agent 循环。

历史实现把同一段约 45 行的循环抄了六遍（`app/agent/orchestrator.py:606-676`
analysis、`:772-833` sql、`:890-958` compute、`:1015-1093` visualize、
`:1157-1212` legacy、`:1267-1303` 内联 explore），加上 `_run_nl_agent`
（`:1096`）这个"无工具"变体共七份。七份之间的差异其实只有配置：
提示词、工具集、迭代上限、要不要向外发正文、要不要注入输入数据。

这里收敛成 `run_agent_loop(spec, ctx, messages)`：六个 agent 只在
`AgentSpec` 上不同（见 `dbchat.agent.specs`）。"回答型 agent"不再是特例，
只是 `max_iterations=1, tools=()`。

三条硬保证：

* **工具不会炸掉循环** —— 任何异常都变成一条失败结果（`ToolRegistry.run`），
  循环继续，模型下一轮能自我纠正；
* **内部字段不出站** —— `_` 前缀的入参只给工具自己，事件与历史都拿不到；
* **取消立即生效** —— 取消信号在流式过程中与工具执行前都被检查。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from dbchat.agent.run import AgentRun
from dbchat.domain.errors import LlmError, LlmUnavailableError
from dbchat.events.agent import (
    AgentEvent,
    AgentFailed,
    AgentFinished,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
)
from dbchat.llm import events as llm_events
from dbchat.llm.messages import ToolCall, assistant_message, tool_result_message
from dbchat.tools.registry import ToolOutcome, ToolSpec, strip_internal_keys

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from dbchat.agent.specs import AgentSpec

__all__ = ["run_agent_loop"]

logger = logging.getLogger(__name__)


async def run_agent_loop(
    spec: AgentSpec,
    ctx: AgentRun,
    messages: Sequence[Any] = (),
) -> AsyncIterator[AgentEvent]:
    """跑一个 agent，产出内部事件。

    Args:
        spec: agent 配置（提示词、工具、迭代上限、钩子）。
        ctx: 本次运行的上下文（`RunContext` + LLM + 工具 + 连接）。
        messages: 起始对话历史（规范 `Message` 或线格式 dict）。

    Yields:
        `ThinkingDelta` / `TextDelta` / `ToolStarted` / `ToolFinished` /
        `AdminQuestionRaised` / `AgentFinished` / `AgentFailed`。

    Notes:
        被取消（`ctx.cancel` 触发或模型回报 `STOP_CANCELLED`）时**静默收尾**：
        不发 `AgentFinished` 也不发 `AgentFailed`，线上语义由用例层决定。
    """
    history: list[Any] = list(messages)
    texts: list[str] = []
    tool_definitions = (
        ctx.registry.definitions(spec.tools, ctx.dialect) if spec.tools else []
    )

    for _iteration in range(max(1, spec.max_iterations)):
        pending: list[ToolCall] = []
        round_text: list[str] = []
        stop_reason = llm_events.STOP_END_TURN

        try:
            async for event in ctx.llm.stream(
                spec.system_prompt, history, tool_definitions, ctx.cancel
            ):
                match event:
                    case llm_events.ThinkingDelta(text=text):
                        if text:
                            yield ThinkingDelta(agent=spec.name, text=text)

                    case llm_events.TextDelta(text=text):
                        if not text:
                            continue
                        texts.append(text)
                        round_text.append(text)
                        if spec.emit_text:
                            yield TextDelta(agent=spec.name, text=text)

                    case llm_events.ToolUse(id=call_id, name=name, input=raw):
                        pending.append(
                            ToolCall(id=call_id, name=name, input=dict(raw or {}))
                        )

                    case llm_events.Done(stop_reason=reason):
                        stop_reason = reason

                    case llm_events.ErrorEvent(message=message, detail=detail):
                        # 流一旦开始就无法抛异常，错误以事件形式给出。
                        yield AgentFailed(
                            agent=spec.name,
                            code="llm_failed",
                            message=message,
                            detail=detail,
                        )
                        return
        except LlmUnavailableError as exc:
            logger.warning("agent %s 不可用: %s", spec.name, exc.detail or exc.user_message)
            yield AgentFailed(
                agent=spec.name,
                code="llm_unavailable",
                message=exc.user_message,
                detail=exc.detail,
            )
            return
        except LlmError as exc:
            logger.warning("agent %s 调用失败: %s", spec.name, exc.detail or exc.user_message)
            yield AgentFailed(
                agent=spec.name,
                code="llm_failed",
                message=exc.user_message,
                detail=exc.detail,
            )
            return

        if _cancelled(ctx):
            logger.info("agent %s 被取消（流式阶段）", spec.name)
            return
        if stop_reason == llm_events.STOP_CANCELLED:
            logger.info("agent %s 被模型端取消", spec.name)
            return
        if not pending:
            break

        history.append(assistant_message("".join(round_text), pending))
        async for event in _execute_tool_round(spec, ctx, pending, history):
            yield event
        if _cancelled(ctx):
            logger.info("agent %s 被取消（工具阶段）", spec.name)
            return

    text = "".join(texts)
    if spec.finish_extract is not None:
        text = await _await_maybe(spec.finish_extract(text))
    yield AgentFinished(agent=spec.name, text=text)


async def _execute_tool_round(
    spec: AgentSpec,
    ctx: AgentRun,
    calls: Sequence[ToolCall],
    history: list[Any],
) -> AsyncIterator[AgentEvent]:
    """执行一轮里的全部工具调用。

    先把所有 `ToolStarted` 发出去（按模型给的顺序），再并发执行，最后按
    同一顺序发 `ToolFinished` —— 事件顺序因此是确定性的（测试与前端都依赖
    这一点），而执行仍然是重叠的。
    """
    tool_ctx = ctx.tool_context()
    prepared: list[tuple[ToolCall, ToolSpec | None, dict[str, Any]]] = []

    for call in calls:
        tool_spec = ctx.registry.get(call.name)
        if tool_spec is None:
            logger.info("模型请求了未注册的工具 %r", call.name)
        enriched = dict(call.input)
        if spec.enrich_input is not None:
            enriched = spec.enrich_input(call.name, enriched)
        # 内部字段（`_input_data` 等）到此为止 —— 事件只拿剥干净的入参。
        yield ToolStarted(
            invocation_id=call.id,
            tool=call.name,
            tool_input=strip_internal_keys(enriched),
        )
        prepared.append((call, tool_spec, enriched))

    limiter = asyncio.Semaphore(ctx.registry.max_parallel)
    outcomes = await asyncio.gather(
        *(
            _invoke(ctx, tool_spec, call, enriched, tool_ctx, limiter)
            for call, tool_spec, enriched in prepared
        )
    )

    for (call, _tool_spec, _enriched), outcome in zip(prepared, outcomes, strict=True):
        if spec.on_tool_result is not None:
            try:
                spec.on_tool_result(call.name, outcome, ctx)
            except Exception as exc:  # noqa: BLE001 - 钩子出错不该中断本轮
                logger.warning(
                    "agent %s 的 on_tool_result 钩子出错: %s: %s",
                    spec.name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
        ctx.record_tool(call.name, outcome)
        for event in outcome.events:
            yield event
        yield ToolFinished(
            invocation_id=call.id,
            tool=call.name,
            ok=outcome.ok,
            summary=outcome.summary,
            payload={
                **outcome.payload,
                "artifacts": outcome.artifacts,
                "error_detail": outcome.error_detail,
            },
            artifacts=outcome.artifacts,
            error_detail=outcome.error_detail,
        )
        history.append(tool_result_message(call.id, outcome.content))


async def _invoke(
    ctx: AgentRun,
    tool_spec: ToolSpec | None,
    call: ToolCall,
    enriched: Mapping[str, Any],
    tool_ctx: Any,
    limiter: asyncio.Semaphore,
) -> ToolOutcome:
    """在并发闸门与超时保护下执行一次工具调用。

    信号量由调用方（每一轮）新建：它必须属于当前运行的事件循环，
    放进模块级全局会在多事件循环（测试、多 worker）下串味。
    """
    timeout = tool_spec.timeout_for(ctx.settings) if tool_spec is not None else None

    try:
        async with limiter:
            return await asyncio.wait_for(
                ctx.registry.run(call.name, enriched, tool_ctx), timeout
            )
    except TimeoutError:
        label = tool_spec.label if tool_spec is not None else call.name
        logger.warning("工具 %s 超时（%s 秒）", call.name, timeout)
        return ToolOutcome.failure(
            f"{label}超时",
            content=f"{label}执行超时（超过 {timeout:g} 秒），请缩小查询范围或改用更简单的处理方式。",
            detail=f"timeout after {timeout:g}s",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：工具层已经处理过一轮，这里保底
        logger.warning(
            "工具 %s 执行异常: %s: %s", call.name, type(exc).__name__, exc, exc_info=True
        )
        return ToolOutcome.failure("工具执行出错", detail=f"{type(exc).__name__}: {exc}")


def _cancelled(ctx: AgentRun) -> bool:
    return llm_events.is_cancelled(ctx.cancel)


async def _await_maybe(value: Any) -> Any:
    """`finish_extract` 允许同步或异步实现。"""
    if hasattr(value, "__await__"):
        return await value
    return value
