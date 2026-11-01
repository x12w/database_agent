"""聊天主管道 —— 五轮迭代 + 计算/绘图 + 自然语言回答。

产出的是**线上事件**（`ChatEvent`）：内部事件（含 SQL、工具名、行数据）在这里
经 `events.project` 投影，API 层直接序列化即可。

从 `app/agent/orchestrator.py:129-455` 移植，删掉了三块：

* **limited mode**（`:230-248` 与整个 `_limited_mode_response` / `_generate_sql` /
  `_fallback_sql` / `_format_limited_response` 家族，`:1826-2100`）—— 那套模板 SQL
  只对培训考试库有意义，换个库就会自信地返回错误答案。现在数据查询没有 API key
  直接以 `llm_unavailable` 失败；纯结构问题仍可读取结构快照作答。
* **legacy agent**（`_run_legacy_agent`，`:1149`）—— 没有任何调用点。
* **查询次数的事件名判断**（`:354`）—— 见 `tools/database.py`。

保留：时间表述的确定性预解析、分类器、结构问题快路径、五轮迭代与结果评估。

agent 内部的失败（`AgentFailed`）不会直接变成线上 `run.failed`：分析 agent 挂掉
只是"这轮没有计划"，不该让整次提问终止。管道把它记进 `AgentRun.state`，按阶段
决定是降级继续还是最终失败。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from dbchat.agent import (
    AgentRun,
    analysis_spec,
    answer_spec,
    build_analysis_history_block,
    build_analysis_user_message,
    build_structure_response,
    classify,
    compute_spec,
    evaluate_results,
    is_simple_query,
    is_structure_question,
    plan_computation,
    resolve_temporal_context,
    run_agent_loop,
    sql_spec,
    strip_sql_from_text,
    visualize_spec,
)
from dbchat.config import Settings, get_settings
from dbchat.db.dialects import get_dialect
from dbchat.domain.errors import DbChatError, DbError, DbErrorKind
from dbchat.events.agent import AgentEvent, AgentFailed, AgentFinished
from dbchat.events.base import (
    RETRYABLE_ERRORS,
    STAGE_LABELS,
    ErrorCode,
    ProfileView,
    RunStats,
    Stage,
    StageView,
)
from dbchat.events.chat import (
    AnswerDelta,
    ArtifactCreated,
    ChatEvent,
    RunFailed,
    RunFinished,
    RunStarted,
    StageFinished,
    StageRetried,
    StageStarted,
)
from dbchat.events.project import RunView, project
from dbchat.llm.client import LLMClient, get_llm_client
from dbchat.llm.messages import assistant_message, user_message
from dbchat.rag.indexer import RAGIndexer
from dbchat.rag.retriever import aretrieve_context
from dbchat.storage.explorations import ExplorationRepository, get_exploration_repository
from dbchat.tools import ToolRegistry, get_registry
from dbchat.usecases.contracts import RunContext
from dbchat.usecases.onboarding import ensure_schema

logger = logging.getLogger(__name__)

__all__ = ["ChatPipeline", "build_chat_use_case", "build_pipeline"]

#: 五个阶段的展示顺序（`run.started` 里告诉前端）。
_STAGES: tuple[Stage, ...] = (
    Stage.UNDERSTANDING,
    Stage.QUERYING,
    Stage.COMPUTING,
    Stage.VISUALIZING,
    Stage.ANSWERING,
)

#: `DbChatError.code` → 线上错误码。
#:
#: 注意两点：错误码有两个来源 —— 异常层（`DbChatError.code`，如 `"db"`）与
#: agent 层（`AgentFailed.code`，已经是线上错误码的取值，如 `"llm_failed"`）。
#: `_error_code()` 先按线上码直接解析，再退回这张表，最后兜底 `internal`。
_ERROR_CODES: dict[str, ErrorCode] = {
    "llm": ErrorCode.LLM_FAILED,
    "llm_unavailable": ErrorCode.LLM_UNAVAILABLE,
    "db": ErrorCode.DB_QUERY_FAILED,
    "validation": ErrorCode.VALIDATION,
    "tool": ErrorCode.INTERNAL,
    "config": ErrorCode.INTERNAL,
    "internal": ErrorCode.INTERNAL,
}

#: `AgentRun.state` 的键：最近一个 agent 的产出文本。
STATE_AGENT_TEXT = "agent_text"
#: `AgentRun.state` 的键：最近一个 agent 的失败信息 `(code, message, detail)`。
STATE_AGENT_ERROR = "agent_error"


class ChatPipeline:
    """聊天主管道（实现 `usecases.contracts.ChatUseCase`）。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        registry: ToolRegistry | None = None,
        explorations: ExplorationRepository | None = None,
        clarifications: Any | None = None,
        schema_store: Any | None = None,
        rag: RAGIndexer | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm = llm or get_llm_client()
        self._registry = registry or get_registry()
        self._explorations = explorations or get_exploration_repository()
        #: 管理员问答仓储（注入 `ask_admin`，让读写落在同一个对象上）。
        self._clarifications = clarifications
        #: 结构快照缓存（可选）。API 层可能已经放了一份在 ctx.snapshot 上。
        self._schema_store = schema_store
        self._rag = rag or RAGIndexer(self._settings, doc_paths=[])

    # ------------------------------------------------------------ 入口

    def run(self, *, message: str, ctx: RunContext) -> AsyncIterator[ChatEvent]:
        """处理一次提问，产出线上事件。"""
        return self._run(message=message, ctx=ctx)

    # ------------------------------------------------------------ 主体

    async def _run(self, *, message: str, ctx: RunContext) -> AsyncIterator[ChatEvent]:
        view = RunView(
            run_id=ctx.run_id,
            now=time.time(),
            expose_sql=self._settings.expose_sql_to_user,
        )
        dialect = get_dialect(ctx.profile.kind)
        answer_parts: list[str] = []
        run = AgentRun(
            ctx=ctx,
            llm=self._llm,
            registry=self._registry,
            settings=self._settings,
            clarifications=self._clarifications,
            rag=self._rag,
        )

        yield RunStarted(
            seq=view.next_seq(),
            ts=time.time(),
            run_id=ctx.run_id,
            profile=ProfileView(
                id=ctx.profile.id,
                name=ctx.profile.display_name,
                dialect_label=dialect.display_name,
            ),
            stages=[StageView(stage=stage, label=STAGE_LABELS[stage]) for stage in _STAGES],
            will_load_schema=ctx.snapshot is None,
        )

        try:
            # ---- 阶段 1：理解问题（结构加载 + 时间解析 + 分类）----
            started = time.monotonic()
            yield self._stage_start(view, Stage.UNDERSTANDING, "加载数据库结构并分析问题")
            structure_only = is_structure_question(message)
            # 数据问题需要模型；先检查配置，避免无意义的连接、内省和探索。
            if not structure_only and not self._llm.available:
                yield self._fail(
                    view,
                    "llm_unavailable",
                    "未配置可用的模型服务，请在 .env 中设置 LLM_API_KEY",
                )
                return

            async for event in ensure_schema(
                ctx,
                settings=self._settings,
                explorations=self._explorations,
                llm=self._llm,
                registry=self._registry,
                clarifications=self._clarifications,
                schema_store=self._schema_store,
                allow_exploration=not structure_only,
                rag=self._rag,
            ):
                yield event
            if ctx.snapshot is None:
                raise DbError(DbErrorKind.UNKNOWN, user_message="无法读取数据库结构")

            # 结构问题只读快照：首次连接也不调用分类模型或探索 agent。
            # 空快照同样是有效结果，由结构回答解释当前可见范围。
            if structure_only:
                yield self._stage_end(
                    view, Stage.UNDERSTANDING, True, self._understanding_summary(ctx), started
                )
                async for event in self._structure_answer(view, ctx, answer_parts):
                    yield event
                yield self._finished(view, ctx, "".join(answer_parts))
                return

            temporal = resolve_temporal_context(message)
            classification = await classify(message, llm=self._llm)
            logger.info(
                "问题分类: %s%s",
                classification,
                "（已解析时间参考）" if temporal else "",
            )
            yield self._stage_end(
                view, Stage.UNDERSTANDING, True, self._understanding_summary(ctx), started
            )

            # ---- 阶段 2：多轮迭代（分析 → 查询 → 评估）----
            async for event in self._query_rounds(view, ctx, run, message, temporal.text):
                yield event

            # ---- 阶段 3/4：计算与绘图 ----
            async for event in self._compute_and_visualize(view, ctx, run, message):
                yield event

            # ---- 阶段 5：自然语言回答（流式）----
            failed: tuple[str, str, str] | None = None
            async for event in self._answer(view, ctx, run, message):
                if isinstance(event, AnswerDelta):
                    answer_parts.append(event.text)
                    yield event
                elif isinstance(event, RunFailed):
                    failed = (event.code.value, event.message, event.error_detail or "")
                else:
                    yield event

            answer = strip_sql_from_text("".join(answer_parts))
            if failed is not None and not answer:
                yield self._fail(view, *failed)
                return
            yield self._finished(view, ctx, answer, partial=failed is not None)

        except DbChatError as exc:
            logger.warning("提问处理失败（%s）: %s", exc.code, exc.detail or exc.user_message)
            yield self._fail(view, exc.code, exc.user_message, exc.detail)
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不把异常抛给 SSE 层
            logger.warning(
                "提问处理出现未预期错误: %s: %s", type(exc).__name__, exc, exc_info=True
            )
            yield self._fail(
                view,
                "internal",
                "处理请求时发生未预期错误，请重试",
                f"{type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------ 各阶段

    async def _structure_answer(
        self, view: RunView, ctx: RunContext, answer_parts: list[str]
    ) -> AsyncIterator[ChatEvent]:
        """结构问题：缓存直接作答（不调模型）。"""
        started = time.monotonic()
        yield self._stage_start(view, Stage.ANSWERING, "结构类问题，直接读取缓存")
        text = build_structure_response(
            ctx.snapshot,
            scope=ctx.scope,
            exploration_report=await self._exploration_report(ctx),
            dialect=get_dialect(ctx.profile.kind),
        )
        for delta in _chunks(text):
            answer_parts.append(delta)
            yield AnswerDelta(seq=view.next_seq(), ts=time.time(), run_id=ctx.run_id, text=delta)
        yield self._stage_end(view, Stage.ANSWERING, True, "已用缓存回答", started)

    async def _query_rounds(
        self,
        view: RunView,
        ctx: RunContext,
        run: AgentRun,
        message: str,
        temporal_text: str,
    ) -> AsyncIterator[ChatEvent]:
        """分析与查询的迭代轮次（最多 `max_query_rounds` 轮）。"""
        settings = self._settings
        effective = f"{temporal_text}\n\n用户问题: {message}" if temporal_text else message
        simple = is_simple_query(message)
        plan = ""
        hint = ""
        report = await self._exploration_report(ctx)

        yield self._stage_start(view, Stage.QUERYING, f"最多 {settings.max_query_rounds} 轮迭代")

        for query_round in range(1, settings.max_query_rounds + 1):
            round_started = time.monotonic()
            if query_round > 1:
                yield StageRetried(
                    seq=view.next_seq(),
                    ts=time.time(),
                    run_id=ctx.run_id,
                    stage=Stage.QUERYING,
                    attempt=query_round,
                    max_attempts=settings.max_query_rounds,
                    reason=hint or "上一轮结果不充分",
                )

            # ---- 分析（首轮；简单问题跳过）----
            if not simple:
                # 缺陷修复 C：重试轮不再是一份空白的分析 agent —— 把上一轮
                # 已经得到的结论（查询计划里的"数据库认识/列名推断"）带过去，
                # 并说明结构已确认，避免每轮重跑一遍 get_schema_summary +
                # describe_table + analyze_column（empty_result 用例里这些
                # 调用在 5 轮中一字不差地重复了 5 遍）。
                retry = query_round > 1
                question = f"{effective}\n\n[上轮提示] {hint}" if retry else effective
                spec = analysis_spec(settings, run.dialect, self._registry)
                analysis_messages = [
                    user_message(
                        build_analysis_user_message(
                            question,
                            history_block=build_analysis_history_block(ctx.history),
                            rag_context=await self._rag_context(question, ctx.profile.id),
                            prior_findings=plan if retry else "",
                            retry=retry,
                        )
                    )
                ]
                async for event in self._drain(view, run, run_agent_loop(spec, run, analysis_messages)):
                    yield event
                error = _take_error(run)
                if error is not None:
                    logger.info("分析 agent 未完成，直接进入查询阶段: %s", error[1])
                    plan = ""
                else:
                    plan = run.state.get(STATE_AGENT_TEXT, "")

            # ---- 查询（SQL agent）----
            spec = await sql_spec(
                settings,
                registry=self._registry,
                dialect=run.dialect,
                snapshot=ctx.snapshot,
                scope=ctx.scope,
                profile_id=ctx.profile.id,
                exploration_report=report,
                analysis_plan=plan,
            )
            messages = [*_history_messages(ctx.history), user_message(effective)]
            results_before = len(ctx.query_results)
            queries_before = ctx.query_count
            async for event in self._drain(view, run, run_agent_loop(spec, run, messages)):
                yield event

            error = _take_error(run)
            if error is not None:
                if query_round < settings.max_query_rounds:
                    hint = f"上一轮查询出错：{error[1]}。请修正 SQL 后重试。"
                    continue
                logger.warning("查询轮次已用尽且最后一轮失败: %s", error[1])
                yield self._stage_end(view, Stage.QUERYING, False, "查询失败", round_started)
                failure = DbChatError(error[1], detail=error[2])
                failure.code = error[0]
                raise failure

            # ---- 评估（缺陷修复 #2：把"这一轮到底查没查"喂进去）----
            evaluation = await evaluate_results(
                ctx.query_results[results_before:],
                message,
                llm=self._llm,
                ran_any_query=ctx.query_count > queries_before,
            )
            if evaluation.satisfactory:
                yield self._stage_end(view, Stage.QUERYING, True, evaluation.reason, round_started)
                return
            if query_round < settings.max_query_rounds:
                hint = evaluation.hint
                logger.info("第 %d 轮结果不充分: %s", query_round, evaluation.reason)
                continue
            logger.info("已尝试全部轮次，基于现有结果回答")
            yield self._stage_end(
                view, Stage.QUERYING, True, "已尝试所有方案，基于现有结果回答", round_started
            )
            return

    async def _compute_and_visualize(
        self, view: RunView, ctx: RunContext, run: AgentRun, message: str
    ) -> AsyncIterator[ChatEvent]:
        """计算与绘图（由规划器决定是否需要）。"""
        plan = await plan_computation(
            message, ctx.query_results, llm=self._llm, ran_any_query=ctx.ran_any_query
        )

        if plan.needs_computation:
            started = time.monotonic()
            yield self._stage_start(view, Stage.COMPUTING, plan.strategy or "计算中")
            spec = compute_spec(
                self._settings,
                registry=self._registry,
                question=message,
                results=ctx.query_results,
                strategy=plan.strategy,
                enable_charts=False,
            )
            async for event in self._drain(
                view,
                run,
                run_agent_loop(spec, run, [user_message("请对以上查询结果进行计算处理。")]),
            ):
                yield event
            error = _take_error(run)
            yield self._stage_end(view, Stage.COMPUTING, error is None, "计算失败" if error else "计算阶段结束", started)
            if error:
                failure = DbChatError(error[1], detail=error[2])
                failure.code = error[0]
                raise failure

        if plan.needs_chart:
            started = time.monotonic()
            yield self._stage_start(view, Stage.VISUALIZING, plan.chart_strategy or "生成图表")
            spec = visualize_spec(
                self._settings,
                registry=self._registry,
                question=message,
                results=ctx.query_results,
                chart_strategy=plan.chart_strategy,
            )
            async for event in self._drain(
                view,
                run,
                run_agent_loop(
                    spec,
                    run,
                    [user_message("请根据以上查询结果生成图表。只生成图表，不进行计算或分析。")],
                ),
            ):
                yield event
            error = _take_error(run)
            yield self._stage_end(view, Stage.VISUALIZING, error is None, "绘图失败" if error else "图表阶段结束", started)
            if error:
                failure = DbChatError(error[1], detail=error[2])
                failure.code = error[0]
                raise failure
            for artifact in ctx.artifacts:
                yield ArtifactCreated(
                    seq=view.next_seq(),
                    ts=time.time(),
                    run_id=ctx.run_id,
                    artifact_id=artifact.artifact_id,
                    kind="chart" if artifact.kind == "chart" else "file",
                    url=artifact.url,
                    title=artifact.title,
                )

    async def _answer(
        self, view: RunView, ctx: RunContext, run: AgentRun, message: str
    ) -> AsyncIterator[ChatEvent]:
        """自然语言回答（流式）。"""
        started = time.monotonic()
        yield self._stage_start(view, Stage.ANSWERING, "生成自然语言回答")
        spec = answer_spec(
            self._settings,
            question=message,
            results=ctx.query_results,
            computed=run.computed,
            artifacts=ctx.artifacts,
        )
        messages = [*_history_messages(ctx.history), user_message(message)]
        async for event in self._drain(view, run, run_agent_loop(spec, run, messages)):
            yield event

        error = _take_error(run)
        if error is not None:
            code, error_message, detail = error
            logger.warning("回答 agent 失败: %s", error_message)
            yield self._fail(view, code, error_message, detail)
            return
        yield self._stage_end(view, Stage.ANSWERING, True, "回答完成", started)

    # ------------------------------------------------------------ 辅助

    async def _drain(
        self, view: RunView, run: AgentRun, events: AsyncIterator[AgentEvent]
    ) -> AsyncIterator[ChatEvent]:
        """把内部事件投影成线上事件。

        `AgentFinished` / `AgentFailed` 是 agent 层的**内部信号**，不直接投影：
        前者用于取回产出文本，后者记进 `run.state` 由本层决定怎么处理
        （分析失败可降级，回答失败才终止）。
        """
        async for event in events:
            if isinstance(event, AgentFinished):
                run.state[STATE_AGENT_TEXT] = event.text
                continue
            if isinstance(event, AgentFailed):
                run.state[STATE_AGENT_ERROR] = (event.code, event.message, event.detail)
                logger.warning("agent %s 失败（%s）: %s", event.agent, event.code, event.message)
                continue
            mapped = project(event, _touch(view))
            if mapped is not None:
                yield mapped

    async def _rag_context(self, question: str, profile_id: str | None = None) -> str:
        try:
            return await aretrieve_context(question, top_k=3, indexer=self._rag, profile_id=profile_id)
        except Exception as exc:  # noqa: BLE001 - 检索失败不该阻断分析
            logger.info("RAG 检索失败: %s: %s", type(exc).__name__, exc)
            return ""

    async def _exploration_report(self, ctx: RunContext) -> str:
        if not ctx.profile.id:
            return ""
        try:
            return await self._explorations.areport(ctx.profile.id)
        except Exception as exc:  # noqa: BLE001 - 报告缺失不影响查询
            logger.info("读取探索报告失败: %s: %s", type(exc).__name__, exc)
            return ""

    def _stats(self, ctx: RunContext) -> RunStats:
        return RunStats(
            queries=ctx.query_count,
            rows=ctx.total_rows,
            charts=sum(1 for artifact in ctx.artifacts if artifact.kind == "chart"),
        )

    def _understanding_summary(self, ctx: RunContext) -> str:
        tables = ctx.snapshot.table_count if ctx.snapshot else 0
        return f"{tables} 张表" if tables else "数据库结构已加载"

    def _stage_start(self, view: RunView, stage: Stage, detail: str) -> StageStarted:
        view.started_at[stage.value] = time.monotonic()
        return StageStarted(
            seq=view.next_seq(),
            ts=time.time(),
            run_id=view.run_id,
            stage=stage,
            label=STAGE_LABELS[stage],
            index=_STAGES.index(stage) + 1,
            total=len(_STAGES),
            detail=detail,
        )

    def _stage_end(
        self, view: RunView, stage: Stage, ok: bool, summary: str, started: float
    ) -> StageFinished:
        return StageFinished(
            seq=view.next_seq(),
            ts=time.time(),
            run_id=view.run_id,
            stage=stage,
            ok=ok,
            summary=summary,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    def _finished(
        self, view: RunView, ctx: RunContext, answer: str, *, partial: bool = False
    ) -> RunFinished:
        return RunFinished(
            seq=view.next_seq(),
            ts=time.time(),
            run_id=ctx.run_id,
            status="partial" if partial else "ok",
            elapsed_ms=ctx.elapsed_ms(),
            stats=self._stats(ctx),
            answer_markdown=answer,
        )

    def _fail(self, view: RunView, code: str, message: str, detail: str = "") -> RunFailed:
        """构造 `run.failed`。"""
        mapped = _error_code(code)
        return RunFailed(
            seq=view.next_seq(),
            ts=time.time(),
            run_id=view.run_id,
            code=mapped,
            message=message,
            retryable=mapped in RETRYABLE_ERRORS,
            error_detail=detail or None,
        )


def build_pipeline(**kwargs) -> ChatPipeline:
    """构造管道（显式传参用；API 层走 `build_chat_use_case`）。"""
    return ChatPipeline(**kwargs)


def build_chat_use_case(ctx: Any) -> ChatPipeline:
    """按 `di.AppContext` 构造聊天用例。

    API 层通过这个名字惰性导入（`dbchat.di._build_use_case`），签名是
    `factory(ctx)` —— 改成别的名字会让路由在运行时 503。
    """
    return ChatPipeline(
        settings=ctx.settings,
        llm=ctx.llm,
        registry=_tools_from(ctx),
        explorations=ctx.explorations,
        clarifications=getattr(ctx, "clarifications", None),
        schema_store=getattr(ctx, "schema_store", None),
        rag=getattr(ctx, "rag", None),
    )


def _tools_from(ctx: Any) -> ToolRegistry:
    """优先用上下文里注入的工具注册表（测试可替换）。"""
    return getattr(ctx, "tools", None) or get_registry()


def _error_code(code: str) -> ErrorCode:
    """把两个来源的错误码归一到线上错误码。"""
    if not code:
        return ErrorCode.INTERNAL
    try:
        return ErrorCode(code)
    except ValueError:
        return _ERROR_CODES.get(code, ErrorCode.INTERNAL)


def _take_error(run: AgentRun) -> tuple[str, str, str] | None:
    """取出并清空最近一次 agent 失败。"""
    return run.state.pop(STATE_AGENT_ERROR, None)


def _touch(view: RunView) -> RunView:
    """刷新投影时间戳 —— `project()` 用 `view.now` 作为事件时间。"""
    view.now = time.time()
    return view


def _chunks(text: str, size: int = 100) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


def _history_messages(history: list[dict]) -> list:
    """把会话历史渲染成规范消息（只保留纯文本问答，丢掉中间态）。"""
    messages: list = []
    for item in history:
        content = item.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if item.get("role") == "assistant":
            messages.append(assistant_message(content))
        elif item.get("role") == "user":
            messages.append(user_message(content))
    return messages
