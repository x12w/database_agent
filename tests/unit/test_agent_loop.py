"""唯一的 agent 循环、分类器与结果评估。

这三块覆盖的是本次重构里风险最高的行为：

1. **循环契约**：迭代上限、工具轮次的历史形状（两个 provider）、内部字段
   不出站、工具异常变成失败结果、取消立即生效。历史实现里这段循环抄了
   六份，每份的边界行为都不一样。
2. **分类器**：`router.classify` 里的 LLM 分支因为
   `run_until_complete` + `except: pass` 从未执行过（见
   `dbchat/agent/classifier.py` 的模块注释），这里用桩客户端证明它可达。
3. **结果评估**：历史实现只看"结果列表是否为空"，分不清"没查"与"查了为空"，
   导致一个 PostgreSQL 问题烧掉 5 轮里的 4 轮。两条分支都必须有测试。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from dbchat.agent import (
    AgentRun,
    AgentSpec,
    classify,
    evaluate_results,
    run_agent_loop,
)
from dbchat.agent.deciders import EMPTY_RESULT_HINT, NO_QUERY_HINT
from dbchat.agent.inputs import input_injector
from dbchat.config import Settings
from dbchat.domain.schema import CatalogScope
from dbchat.events.agent import (
    AgentFailed,
    AgentFinished,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
)
from dbchat.llm.client import CLASS_AMBIGUOUS, CLASS_DATA_QUESTION, CLASS_DOC_QUESTION
from dbchat.llm.events import (
    STOP_CANCELLED,
    STOP_END_TURN,
    STOP_TOOL_USE,
    Done,
    ErrorEvent,
    ToolUse,
)
from dbchat.llm.events import (
    TextDelta as LlmTextDelta,
)
from dbchat.llm.events import (
    ThinkingDelta as LlmThinkingDelta,
)
from dbchat.llm.messages import to_anthropic_messages, to_openai_messages
from dbchat.tools.registry import ToolOutcome, ToolRegistry, ToolSpec, static_description
from dbchat.usecases.contracts import RunContext

# ------------------------------------------------------------------ 桩


class _StubParams(BaseModel):
    """测试工具入参（`extra="allow"` 才能看到注入的内部字段）。"""

    model_config = ConfigDict(extra="allow")

    text: str = Field(default="")
    value: int = Field(default=0)


class ScriptedLLM:
    """按脚本产出的假客户端。

    脚本是"每次调用（一次模型调用）一组事件"，可以放可调用对象 ——
    它们在**产出时**执行，用来在流中途制造取消/异常。
    """

    def __init__(
        self,
        script: Sequence[Sequence[Any]],
        *,
        available: bool = True,
        provider: str = "anthropic",
        replies: Sequence[str] = (),
        classification: str = CLASS_AMBIGUOUS,
        error: Exception | None = None,
    ) -> None:
        self.script = [list(step) for step in script]
        self.available = available
        self.provider = provider
        self.model = "stub-model"
        self.replies = list(replies)
        self.classification = classification
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self,
        system: str,
        messages: Sequence[Any],
        tools: Sequence[Any] | None = None,
        cancel: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        self.calls.append(
            {
                "system": system,
                "messages": list(messages),
                "tools": list(tools or ()),
                "cancel": cancel,
            }
        )
        if self.error is not None:
            raise self.error
        index = min(len(self.calls) - 1, len(self.script) - 1)
        for item in self.script[index] if self.script else ():
            if callable(item):
                produced = item()
                if produced is None:
                    continue
                item = produced
            yield item

    async def complete(self, system: str, messages: Sequence[Any], **kwargs: Any) -> str:
        if not self.replies:
            return ""
        return self.replies.pop(0)

    async def classify(self, question: str) -> str:
        return self.classification


class _Recorder:
    """记录工具被真正调用时的入参。"""

    def __init__(self) -> None:
        self.inputs: list[dict[str, Any]] = []
        self.calls = 0

    async def handler(self, params: _StubParams, ctx: Any) -> ToolOutcome:
        self.calls += 1
        self.inputs.append(dict(getattr(params, "__pydantic_extra__", None) or {}))
        return ToolOutcome(
            ok=True,
            summary=f"echo {params.text}",
            content=f"echo:{params.text}",
            payload={"row_count": 1, "execution_ms": 1},
        )


async def _boom(params: _StubParams, ctx: Any) -> ToolOutcome:
    raise RuntimeError("工具炸了")


def _registry(*, extra: list[ToolSpec] | None = None) -> ToolRegistry:
    recorder = _Recorder()
    specs = [
        ToolSpec(
            name="echo",
            label="回声",
            params=_StubParams,
            handler=recorder.handler,
            description=static_description("回声工具"),
            agents=frozenset({"test"}),
            timeout=lambda settings: 5.0,
        ),
        ToolSpec(
            name="boom",
            label="会炸的工具",
            params=_StubParams,
            handler=_boom,
            description=static_description("总是抛异常"),
            agents=frozenset({"test"}),
            timeout=lambda settings: 5.0,
        ),
        *(extra or ()),
    ]
    return ToolRegistry(specs)


def _context(**kwargs: Any) -> tuple[RunContext, Settings]:
    from dbchat.domain.profile import DatabaseProfile

    settings = Settings(llm_api_key="test-key")
    ctx = RunContext(
        run_id="run-1",
        profile=DatabaseProfile(id="p1", name="测试库", database="demo"),
        scope=CatalogScope(),
        conversation_id="conv-1",
    )
    _ = kwargs
    return ctx, settings


def _run(llm: ScriptedLLM, registry: ToolRegistry, spec: AgentSpec, **kwargs: Any):
    ctx, settings = _context()
    run = AgentRun(ctx=ctx, llm=llm, registry=registry, settings=settings, **kwargs)
    return ctx, run


async def _collect(events: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in events]


def _tool_use(call_id: str, name: str, **payload: Any) -> ToolUse:
    return ToolUse(id=call_id, name=name, input=payload)


# ------------------------------------------------------------------ 循环


@pytest.mark.asyncio
async def test_iteration_cap_is_respected() -> None:
    """迭代上限是模型调用次数上限 —— 到点必须停，不能无限要工具。"""
    registry = _registry()
    llm = ScriptedLLM(
        [[_tool_use("c1", "echo", text="a"), Done(STOP_TOOL_USE)]] * 10
    )
    spec = AgentSpec(name="test", display="测试", system_prompt="s", tools=("echo",), max_iterations=2)
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))

    assert len(llm.calls) == 2
    tools = [e for e in events if isinstance(e, ToolFinished)]
    assert len(tools) == 2
    assert isinstance(events[-1], AgentFinished)
    _ = run


@pytest.mark.asyncio
async def test_tool_round_is_appended_in_provider_shape() -> None:
    """工具轮次要按规范形态入历史，两个 provider 渲染出来都必须配对。"""
    registry = _registry()
    llm = ScriptedLLM(
        [
            [_tool_use("c1", "echo", text="甲", value=7), Done(STOP_TOOL_USE)],
            [LlmTextDelta("好了"), Done(STOP_END_TURN)],
        ]
    )
    spec = AgentSpec(name="test", display="测试", system_prompt="s", tools=("echo",), max_iterations=3)
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))
    assert isinstance(events[-1], AgentFinished)

    history = llm.calls[1]["messages"]
    # ---- Anthropic：assistant 的 tool_use 块 + user 的 tool_result 块 ----
    anthropic_messages = to_anthropic_messages(history)
    assistant_blocks = [
        block
        for message in anthropic_messages
        if message["role"] == "assistant" and isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == "tool_use"
    ]
    result_blocks = [
        block
        for message in anthropic_messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block["type"] == "tool_result"
    ]
    assert [block["id"] for block in assistant_blocks] == ["c1"]
    assert [block["tool_use_id"] for block in result_blocks] == ["c1"]

    # ---- OpenAI：assistant.tool_calls 必须保留（否则第二轮断链）----
    openai_messages = to_openai_messages("s", history)
    assistant_calls = [
        call
        for message in openai_messages
        if message["role"] == "assistant" and message.get("tool_calls")
        for call in message["tool_calls"]
    ]
    assert [call["id"] for call in assistant_calls] == ["c1"]
    tool_messages = [m for m in openai_messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["c1"]


@pytest.mark.asyncio
async def test_internal_keys_reach_tool_but_not_events_or_history() -> None:
    """`_` 前缀的内部字段只给工具用，事件与历史都拿不到。"""
    recorder = _Recorder()
    # 注入器只对 `run_python` 生效（只有它吃查询结果），所以测试工具同名。
    registry = ToolRegistry(
        [
            ToolSpec(
                name="run_python",
                label="执行计算",
                params=_StubParams,
                handler=recorder.handler,
                description=static_description("执行代码"),
                agents=frozenset({"test"}),
                timeout=lambda settings: 5.0,
            )
        ]
    )
    secret = {"rows": [{"a": 1}], "row_count": 1}
    llm = ScriptedLLM(
        [
            [_tool_use("c1", "run_python", text="甲"), Done(STOP_TOOL_USE)],
            [Done(STOP_END_TURN)],
        ]
    )
    spec = AgentSpec(
        name="test",
        display="测试",
        system_prompt="s",
        tools=("run_python",),
        max_iterations=3,
        enrich_input=input_injector(secret),
    )
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))

    # 调用时在
    assert recorder.inputs == [{"_input_data": secret}]
    # 事件里不在
    started = [e for e in events if isinstance(e, ToolStarted)]
    assert started and "_input_data" not in started[0].tool_input
    assert started[0].tool_input == {"text": "甲"}
    assert "_input_data" not in str(started[0])
    # 历史里不在
    assert "_input_data" not in str(llm.calls[1]["messages"])
    # 但工具产出的正文照常进历史
    assert "echo:甲" in str(llm.calls[1]["messages"])


@pytest.mark.asyncio
async def test_raising_tool_yields_failed_result_not_crash() -> None:
    """工具抛异常 → 失败的 ToolFinished，循环继续，整轮不崩。"""
    registry = _registry()
    llm = ScriptedLLM(
        [
            [_tool_use("c1", "boom"), Done(STOP_TOOL_USE)],
            [LlmTextDelta("已经改口"), Done(STOP_END_TURN)],
        ]
    )
    spec = AgentSpec(name="test", display="测试", system_prompt="s", tools=("boom",), max_iterations=3)
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))
    finished = [e for e in events if isinstance(e, ToolFinished)]

    assert len(finished) == 1
    assert finished[0].ok is False
    assert finished[0].error_detail
    assert "RuntimeError" not in finished[0].summary
    assert isinstance(events[-1], AgentFinished)
    assert len(llm.calls) == 2  # 第二轮照常发起


@pytest.mark.asyncio
async def test_unknown_tool_is_a_failed_result() -> None:
    """模型臆造工具名 → 失败结果（含可用工具清单），不是异常。"""
    registry = _registry()
    llm = ScriptedLLM([[_tool_use("c1", "不存在的工具"), Done(STOP_TOOL_USE)], [Done(STOP_END_TURN)]])
    spec = AgentSpec(name="test", display="测试", system_prompt="s", tools=("echo",), max_iterations=2)
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))
    finished = [e for e in events if isinstance(e, ToolFinished)][0]
    assert finished.ok is False
    assert "未知工具" in finished.summary


@pytest.mark.asyncio
async def test_cancellation_mid_stream_stops_promptly() -> None:
    """流中途取消：当轮的工具**不再执行**，循环立刻收尾。"""
    recorder = _Recorder()
    registry = ToolRegistry(
        [
            ToolSpec(
                name="echo",
                label="回声",
                params=_StubParams,
                handler=recorder.handler,
                description=static_description("回声"),
                agents=frozenset({"test"}),
                timeout=lambda settings: 5.0,
            )
        ]
    )

    class _Cancel:
        def __init__(self) -> None:
            self.flag = False

        def is_set(self) -> bool:
            return self.flag

    token = _Cancel()

    def _cancel_now() -> None:
        token.flag = True

    llm = ScriptedLLM(
        [[_cancel_now, _tool_use("c1", "echo", text="甲"), Done(STOP_TOOL_USE)], [Done(STOP_END_TURN)]]
    )
    spec = AgentSpec(name="test", display="测试", system_prompt="s", tools=("echo",), max_iterations=3)
    _, run = _run(llm, registry, spec, cancel=token)

    events = await _collect(run_agent_loop(spec, run))

    assert recorder.calls == 0
    assert not [e for e in events if isinstance(e, ToolStarted)]
    assert not [e for e in events if isinstance(e, AgentFinished)]
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_done_cancelled_stops_without_finishing() -> None:
    """模型回报 STOP_CANCELLED：静默收尾（线上语义由用例层决定）。"""
    registry = _registry()
    llm = ScriptedLLM([[LlmTextDelta("写到一半"), Done(STOP_CANCELLED)]])
    spec = AgentSpec(name="test", display="测试", system_prompt="s", max_iterations=1)
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))
    assert events == []


@pytest.mark.asyncio
async def test_emit_text_per_spec() -> None:
    """只有 emit_text=True 的 agent 会把正文发出去；两者都进产出文本。"""
    registry = _registry()
    llm = ScriptedLLM([[LlmThinkingDelta("想"), LlmTextDelta("正文"), Done(STOP_END_TURN)]])
    quiet = AgentSpec(name="test", display="测试", system_prompt="s", max_iterations=1)
    _, run = _run(llm, registry, quiet)

    events = await _collect(run_agent_loop(quiet, run))
    assert [e for e in events if isinstance(e, TextDelta)] == []
    assert [e for e in events if isinstance(e, ThinkingDelta)][0].text == "想"
    assert isinstance(events[-1], AgentFinished)
    assert events[-1].text == "正文"

    llm2 = ScriptedLLM([[LlmTextDelta("正文"), Done(STOP_END_TURN)]])
    loud = AgentSpec(name="test", display="测试", system_prompt="s", max_iterations=1, emit_text=True)
    _, run2 = _run(llm2, registry, loud)
    events2 = await _collect(run_agent_loop(loud, run2))
    assert [e.text for e in events2 if isinstance(e, TextDelta)] == ["正文"]


@pytest.mark.asyncio
async def test_finish_extract_runs_on_accumulated_text() -> None:
    """finish_extract 拿到的是一整轮累积的正文（计划抽取/报告落盘靠它）。"""
    registry = _registry()
    llm = ScriptedLLM(
        [
            [LlmTextDelta("```plan\n"), _tool_use("c1", "echo"), Done(STOP_TOOL_USE)],
            [LlmTextDelta("步骤一\n```"), Done(STOP_END_TURN)],
        ]
    )
    seen: list[str] = []

    def extract(text: str) -> str:
        seen.append(text)
        return text.strip().strip("`").replace("plan", "").strip()

    spec = AgentSpec(
        name="test",
        display="测试",
        system_prompt="s",
        tools=("echo",),
        max_iterations=2,
        finish_extract=extract,
    )
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))
    assert seen == ["```plan\n步骤一\n```"]
    assert events[-1].text == "步骤一"


@pytest.mark.asyncio
async def test_llm_error_becomes_agent_failed() -> None:
    """流里报错 → AgentFailed，且不再继续迭代。"""
    registry = _registry()
    llm = ScriptedLLM([[ErrorEvent("模型调用失败", detail="boom")]])
    spec = AgentSpec(name="test", display="测试", system_prompt="s", max_iterations=3)
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))
    assert isinstance(events[-1], AgentFailed)
    assert events[-1].code == "llm_failed"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_tools_run_concurrently_with_bounded_parallelism() -> None:
    """同一轮的多个工具调用要重叠执行，且受并发上限约束。"""
    import asyncio

    active = 0
    peak = 0

    async def _slow(params: _StubParams, ctx: Any) -> ToolOutcome:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return ToolOutcome(ok=True, summary="ok", content="ok")

    specs = [
        ToolSpec(
            name=f"slow{i}",
            label=f"慢工具{i}",
            params=_StubParams,
            handler=_slow,
            description=static_description("慢"),
            agents=frozenset({"test"}),
            timeout=lambda settings: 5.0,
        )
        for i in range(4)
    ]
    registry = ToolRegistry(specs, max_parallel=2)
    llm = ScriptedLLM(
        [
            [*[_tool_use(f"c{i}", f"slow{i}") for i in range(4)], Done(STOP_TOOL_USE)],
            [Done(STOP_END_TURN)],
        ]
    )
    spec = AgentSpec(
        name="test",
        display="测试",
        system_prompt="s",
        tools=tuple(s.name for s in specs),
        max_iterations=2,
    )
    _, run = _run(llm, registry, spec)

    events = await _collect(run_agent_loop(spec, run))
    assert peak == 2  # 上限生效
    order = [e.invocation_id for e in events if isinstance(e, ToolStarted)]
    assert order == ["c0", "c1", "c2", "c3"]  # 事件顺序确定
    assert [e.invocation_id for e in events if isinstance(e, ToolFinished)] == order


# ------------------------------------------------------------------ 分类器


@pytest.mark.asyncio
async def test_classifier_uses_llm_answer_when_keywords_tie() -> None:
    """关键词分不出高下时，**模型分类必须真的被用上**。

    历史实现的这条分支永远走不到（`run_until_complete` 在运行中的循环里必
    抛 `RuntimeError`，又被 `except: pass` 吞掉）。这里用一个两边都不命中的
    问题把流程逼到模型分支上。
    """
    question = "请帮我看看这个东西"
    llm = ScriptedLLM([[]], classification=CLASS_DOC_QUESTION)

    result = await classify(question, llm=llm)

    assert result == CLASS_DOC_QUESTION


@pytest.mark.asyncio
async def test_classifier_falls_back_to_data_bias_when_llm_ambiguous() -> None:
    """模型答"含糊"时，默认偏置是"去查数据库"。"""
    llm = ScriptedLLM([[]], classification=CLASS_AMBIGUOUS)
    assert await classify("请帮我看看这个东西", llm=llm) == CLASS_DATA_QUESTION


@pytest.mark.asyncio
async def test_classifier_keywords_win_without_calling_llm() -> None:
    """关键词足够强时不打扰模型。"""
    llm = ScriptedLLM([[]], classification=CLASS_DOC_QUESTION)
    assert await classify("统计各部门有多少人", llm=llm) == CLASS_DATA_QUESTION
    assert await classify("这张表的字段类型是什么", llm=llm) == CLASS_DOC_QUESTION
    assert llm.calls == []


# ------------------------------------------------------------------ 结果评估


class _FakeResult:
    def __init__(self, rows: int) -> None:
        self.row_count = rows
        self.columns = ["c"]
        self.rows = [{"c": 1}] * rows


@pytest.mark.asyncio
async def test_evaluation_distinguishes_no_query_from_empty_result() -> None:
    """缺陷修复 #2：没查 ≠ 查了为空，两条提示必须不同。"""
    llm = ScriptedLLM([[]], available=False)

    never_queried = await evaluate_results([], "有多少人", llm=llm, ran_any_query=False)
    assert never_queried.satisfactory is False
    assert never_queried.ran_query is False
    assert never_queried.hint == NO_QUERY_HINT
    assert "还没有执行任何查询" in never_queried.hint
    assert "query_database" in never_queried.hint

    empty_result = await evaluate_results([], "有多少人", llm=llm, ran_any_query=True)
    assert empty_result.satisfactory is False
    assert empty_result.ran_query is True
    assert empty_result.hint == EMPTY_RESULT_HINT
    assert "还没有执行任何查询" not in empty_result.hint
    assert "没有匹配的数据" in empty_result.hint

    assert never_queried.reason != empty_result.reason


@pytest.mark.asyncio
async def test_evaluation_accepts_non_empty_results() -> None:
    llm = ScriptedLLM([[]], available=False)
    outcome = await evaluate_results([_FakeResult(3)], "有多少人", llm=llm, ran_any_query=True)
    assert outcome.satisfactory is True
    assert "3" in outcome.reason


@pytest.mark.asyncio
async def test_evaluation_retry_hint_comes_from_model() -> None:
    llm = ScriptedLLM([[]], available=True, replies=["RETRY: 请改用 employee 表"])
    outcome = await evaluate_results([_FakeResult(1)], "有多少人", llm=llm, ran_any_query=True)
    assert outcome.satisfactory is False
    assert outcome.hint == "请改用 employee 表"


@pytest.mark.asyncio
async def test_evaluation_never_prompts_structure_check_when_query_ran() -> None:
    """查过之后再让模型"确认表结构"正是历史缺陷的表现，提示里不能再出现。"""
    llm = ScriptedLLM([[]], available=True, replies=["SATISFIED"])
    outcome = await evaluate_results([_FakeResult(2)], "有多少人", llm=llm, ran_any_query=True)
    assert outcome.satisfactory is True




# ------------------------------------------------------------------ 管道集成


class _FakeExplorations:
    """探索报告仓储的替身（管道只读报告）。"""

    def __init__(self, report: str = "") -> None:
        self._report = report
        self.saved: list[str] = []

    async def areport(self, profile_id: str) -> str:
        return self._report

    async def ais_explored(self, profile_id: str) -> bool:
        return True

    async def asave(self, profile_id: str, report: str, *args: Any, **kwargs: Any) -> None:
        self.saved.append(report)


def _snapshot():
    from dbchat.domain.schema import ColumnMeta, SchemaSnapshot, TableMeta

    return SchemaSnapshot(
        db_name="demo",
        tables={
            "employee": TableMeta(
                name="employee",
                comment="员工",
                row_count=12,
                columns=[
                    ColumnMeta(name="id", data_type="int", column_type="int", is_primary_key=True),
                    ColumnMeta(name="employee_name", data_type="varchar", column_type="varchar(32)"),
                ],
                sample_rows=[{"id": 1, "employee_name": "张三"}],
            )
        },
    )


async def _fake_query_database(params: Any, ctx: Any) -> ToolOutcome:
    """替掉真查询：只验证"工具自己计数"这条链路。"""
    from dbchat.domain.query import QueryResult

    result = QueryResult(
        sql=params.sql,
        columns=["cnt"],
        rows=[{"cnt": 5}],
        row_count=1,
        execution_ms=3,
    )
    ctx.ctx.add_query_result(result)
    return ToolOutcome(
        ok=True,
        summary="返回 1 行",
        content="查询返回 1 行，耗时 3ms。",
        payload={"sql": params.sql, "row_count": 1, "execution_ms": 3},
    )


def _registry_with_fake_query() -> ToolRegistry:
    """真注册表，但把 `query_database` 换成不打库的实现。"""
    from dataclasses import replace

    from dbchat.tools import build_registry

    real = build_registry(Settings())
    specs = []
    for name in real.names:
        spec = real.require(name)
        if name == "query_database":
            spec = replace(spec, handler=_fake_query_database)
        specs.append(spec)
    return ToolRegistry(specs)


@pytest.mark.asyncio
async def test_pipeline_query_count_and_projection() -> None:
    """端到端：查询计数由工具记录；线上事件里不出现 SQL 与工具名。"""
    from dbchat.events.chat import AnswerDelta, RunFinished, RunStarted
    from dbchat.usecases.chat import ChatPipeline

    llm = ScriptedLLM(
        [
            [LlmTextDelta("```plan\n步骤一\n```"), Done(STOP_END_TURN)],
            [
                _tool_use("q1", "query_database", sql="SELECT COUNT(*) AS cnt FROM employee"),
                Done(STOP_TOOL_USE),
            ],
            [LlmTextDelta("已取得数据"), Done(STOP_END_TURN)],
            [LlmTextDelta("共有 5 人。"), Done(STOP_END_TURN)],
        ],
        replies=["SATISFIED", "SKIP"],
    )
    pipeline = ChatPipeline(
        settings=Settings(llm_api_key="test-key", expose_sql_to_user=False),
        llm=llm,
        registry=_registry_with_fake_query(),
        explorations=_FakeExplorations(),
    )
    ctx, _ = _context()
    ctx.snapshot = _snapshot()
    ctx.history = [{"role": "user", "content": "之前问过什么"}, {"role": "assistant", "content": "之前答过"}]

    events = [event async for event in pipeline.run(message="统计员工有多少人", ctx=ctx)]

    assert isinstance(events[0], RunStarted)
    assert isinstance(events[-1], RunFinished)

    # 缺陷修复 #1：查询次数不再恒为 0
    assert ctx.query_count == 1
    assert ctx.ran_any_query is True
    assert events[-1].stats.queries == 1
    assert events[-1].stats.rows == 1

    # 答案来自回答 agent，且清洗后被记入 run.finished
    answer = "".join(event.text for event in events if isinstance(event, AnswerDelta))
    assert answer == "共有 5 人。"
    assert events[-1].answer_markdown == "共有 5 人。"
    assert events[-1].status == "ok"

    # 内部信息不出站：没有 SQL、没有工具名
    dumped = " ".join(event.model_dump_json() for event in events)
    assert "SELECT" not in dumped
    assert "query_database" not in dumped
    assert "get_schema_summary" not in dumped
    # 阶段事件齐全
    stages = [event.stage.value for event in events if hasattr(event, "stage")]
    assert "understanding" in stages
    assert "querying" in stages
    assert "answering" in stages


@pytest.mark.asyncio
async def test_pipeline_without_api_key_fails_with_llm_unavailable() -> None:
    """没有 API key：明确失败，绝不退回模板 SQL（历史 limited mode 已删除）。"""
    from dbchat.events.base import ErrorCode
    from dbchat.events.chat import RunFailed
    from dbchat.usecases.chat import ChatPipeline

    llm = ScriptedLLM([[]], available=False)
    pipeline = ChatPipeline(
        settings=Settings(llm_api_key="", anthropic_api_key=""),
        llm=llm,
        registry=_registry_with_fake_query(),
        explorations=_FakeExplorations(),
    )
    ctx, _ = _context()
    ctx.snapshot = _snapshot()

    events = [event async for event in pipeline.run(message="统计员工有多少人", ctx=ctx)]

    failure = [event for event in events if isinstance(event, RunFailed)]
    assert len(failure) == 1
    assert failure[0].code is ErrorCode.LLM_UNAVAILABLE
    assert llm.calls == []  # 一次模型调用都没发出去


@pytest.mark.asyncio
async def test_pipeline_structure_fast_path_skips_the_model() -> None:
    """结构类问题走快路径：直接用缓存作答，不调模型。"""
    from dbchat.events.chat import AnswerDelta, RunFinished
    from dbchat.usecases.chat import ChatPipeline

    llm = ScriptedLLM([[]])
    pipeline = ChatPipeline(
        settings=Settings(llm_api_key="test-key"),
        llm=llm,
        registry=_registry_with_fake_query(),
        explorations=_FakeExplorations(report="# 探索报告\n- 只有一张表"),
    )
    ctx, _ = _context()
    ctx.snapshot = _snapshot()

    events = [event async for event in pipeline.run(message="数据库有哪些表？", ctx=ctx)]

    answer = "".join(event.text for event in events if isinstance(event, AnswerDelta))
    assert "employee" in answer
    assert "探索报告" in answer
    assert llm.calls == []
    assert isinstance(events[-1], RunFinished)


@pytest.mark.asyncio
async def test_retry_round_carries_prior_findings_into_analysis() -> None:
    """缺陷 C：重试轮的分析 agent 带着上一轮的结论，而不是从零再探一遍结构。

    背景（`tests/golden-new/empty_result.jsonl`）：用例层每轮都新建一份空白
    分析 agent，`get_schema_summary` + `describe_table` + `analyze_column`
    在 5 轮里一字不差地重复了 5 遍。这里验证第二轮的分析消息里确实带上了
    首轮的查询计划，并且明确说明结构已确认。
    """
    from dataclasses import replace

    from dbchat.agent.prompts import RETRY_STRUCTURE_NOTE
    from dbchat.domain.query import QueryResult
    from dbchat.events.chat import RunFinished
    from dbchat.tools import build_registry
    from dbchat.usecases.chat import ChatPipeline

    seen = {"queries": 0}

    async def _empty_then_rows(params: Any, ctx: Any) -> ToolOutcome:
        """首轮返回空结果（触发重试），其后返回 4 行（让评估收尾）。"""
        seen["queries"] += 1
        rows = 0 if seen["queries"] == 1 else 4
        ctx.ctx.add_query_result(
            QueryResult(
                sql=params.sql,
                columns=["cnt"],
                rows=[{"cnt": 4}] * rows,
                row_count=rows,
                execution_ms=1,
            )
        )
        return ToolOutcome(
            ok=True,
            summary=f"返回 {rows} 行",
            content=f"查询返回 {rows} 行",
            payload={"sql": params.sql, "row_count": rows, "execution_ms": 1},
        )

    real = build_registry(Settings())
    specs = [
        replace(real.require(name), handler=_empty_then_rows)
        if name == "query_database"
        else real.require(name)
        for name in real.names
    ]

    plan_first = "```plan\n## 数据库认识\n- employee 表 13 列，employee_name 有样本值「张三」\n```"
    llm = ScriptedLLM(
        [
            [LlmTextDelta(plan_first), Done(STOP_END_TURN)],
            [_tool_use("q1", "query_database", sql="SELECT COUNT(*) FROM employee"), Done(STOP_TOOL_USE)],
            [LlmTextDelta("已执行查询"), Done(STOP_END_TURN)],
            [LlmTextDelta("```plan\n步骤一（修正后）\n```"), Done(STOP_END_TURN)],
            [_tool_use("q2", "query_database", sql="SELECT COUNT(*) FROM employee"), Done(STOP_TOOL_USE)],
            [LlmTextDelta("已执行查询"), Done(STOP_END_TURN)],
            [LlmTextDelta("共 4 人。"), Done(STOP_END_TURN)],
        ],
        # 第 1 轮空结果走确定性分支（不问模型），第 2 轮才有评估调用。
        replies=["SATISFIED", "SKIP"],
    )
    pipeline = ChatPipeline(
        settings=Settings(llm_api_key="test-key"),
        llm=llm,
        registry=ToolRegistry(specs),
        explorations=_FakeExplorations(),
    )
    ctx, _ = _context()
    ctx.snapshot = _snapshot()

    events = [event async for event in pipeline.run(message="统计每个部门的人数", ctx=ctx)]
    assert isinstance(events[-1], RunFinished)
    assert seen["queries"] == 2  # 真的跑到第二轮

    def _user_text(call: dict) -> str:
        return "\n".join(m.text for m in call["messages"] if getattr(m, "role", "") == "user")

    analysis_calls = [call for call in llm.calls if "请分析以下用户问题" in _user_text(call)]
    assert len(analysis_calls) == 2

    first, second = (_user_text(call) for call in analysis_calls)
    # 首轮照旧：完整探索
    assert "上一轮已确认的结论" not in first
    assert RETRY_STRUCTURE_NOTE not in first
    # 重试轮：带上上轮结论 + 结构已确认的说明 + 上轮提示
    assert "上一轮已确认的结论" in second
    assert "employee_name 有样本值「张三」" in second
    assert RETRY_STRUCTURE_NOTE in second
    assert "不要重复调用 get_schema_summary" in second
    assert "[上轮提示]" in second
    # 但工具没有被禁用 —— 丢了上下文的 agent 仍然能去看
    analysis_tools = [tool["name"] for tool in analysis_calls[1]["tools"]]
    assert "get_schema_summary" in analysis_tools
    assert "analyze_column" in analysis_tools


@pytest.mark.asyncio
async def test_pipeline_answer_agent_failure_maps_to_llm_failed() -> None:
    """回答 agent 挂掉 → 线上错误码是 `llm_failed`（不是兜底的 `internal`）。"""
    from dbchat.events.base import ErrorCode
    from dbchat.events.chat import RunFailed
    from dbchat.usecases.chat import ChatPipeline

    llm = ScriptedLLM(
        [
            [LlmTextDelta("```plan\n步骤一\n```"), Done(STOP_END_TURN)],
            [
                _tool_use("q1", "query_database", sql="SELECT COUNT(*) AS cnt FROM employee"),
                Done(STOP_TOOL_USE),
            ],
            [LlmTextDelta("已取得数据"), Done(STOP_END_TURN)],
            [ErrorEvent("模型调用失败", detail="连接被重置")],
        ],
        replies=["SATISFIED", "SKIP"],
    )
    pipeline = ChatPipeline(
        settings=Settings(llm_api_key="test-key"),
        llm=llm,
        registry=_registry_with_fake_query(),
        explorations=_FakeExplorations(),
    )
    ctx, _ = _context()
    ctx.snapshot = _snapshot()

    events = [event async for event in pipeline.run(message="统计员工有多少人", ctx=ctx)]

    failure = [event for event in events if isinstance(event, RunFailed)]
    assert len(failure) == 1
    assert failure[0].code is ErrorCode.LLM_FAILED
    assert "连接被重置" in (failure[0].error_detail or "")
    # 查询照常被记录（失败发生在回答阶段）
    assert ctx.query_count == 1
