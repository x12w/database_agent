"""六个 agent 的配置 —— 它们是同一段循环的不同参数。

| agent | 工具 | 迭代上限 | 说明 |
| --- | --- | --- | --- |
| analysis | 结构与检索类，**不含** `query_database` | `max_analysis_iterations` | 只探索、产出 ```plan``` |
| sql | 查询 + 结构 + 检索 | `max_sql_iterations` | 真正取数 |
| compute | `run_python` | `max_compute_iterations` | 注入查询结果做计算 |
| visualize | `run_python` | `max_visualization_iterations` | 注入查询结果画图 |
| answer | 无 | 1 | 只把结果说清楚 |
| explore | 结构 + 查询 + 计算 | `max_sql_iterations` | 首次连接时探索整库 |

"回答型 agent"因此不再是代码里的特例：`max_iterations=1, tools=()`
就是它全部的特殊之处（历史实现为它单独写了一个 `_run_nl_agent`）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from dbchat.agent.inputs import build_query_input_data, input_injector
from dbchat.agent.prompts import (
    build_analysis_prompt,
    build_answer_prompt,
    build_compute_prompt,
    build_explore_prompt,
    build_schema_context,
    build_sql_prompt,
    build_visualize_prompt,
    extract_table_refs,
)
from dbchat.agent.run import AgentRun
from dbchat.config import Settings
from dbchat.db.dialects.base import Dialect
from dbchat.domain.query import Artifact, QueryResult
from dbchat.domain.schema import CatalogScope, SchemaSnapshot
from dbchat.tools import (
    AGENT_ANALYSIS,
    AGENT_ANSWER,
    AGENT_COMPUTE,
    AGENT_EXPLORE,
    AGENT_SQL,
    AGENT_VISUALIZE,
    ToolOutcome,
)
from dbchat.tools.registry import ToolRegistry

__all__ = [
    "AgentSpec",
    "analysis_spec",
    "answer_spec",
    "build_specs",
    "compute_spec",
    "explore_spec",
    "extract_plan",
    "spec_tools",
    "sql_spec",
    "visualize_spec",
]

logger = logging.getLogger(__name__)

#: `finish_extract` 的形状：把整轮累积的正文变成 agent 的产出。
FinishExtract = Callable[[str], str | Awaitable[str]]
#: `enrich_input` 的形状：给工具入参补齐内部字段。
EnrichInput = Callable[[str, dict[str, Any]], dict[str, Any]]
#: `on_tool_result` 的形状：旁路收集工具结果。
OnToolResult = Callable[[str, ToolOutcome, AgentRun], None]


@dataclass(slots=True, frozen=True)
class AgentSpec:
    """一个 agent 的配置。"""

    name: str
    #: 展示名（日志与排查用；线上事件不带 agent 名）。
    display: str
    #: 系统提示词（由 `agent.prompts` 的构建函数产出）。
    system_prompt: str
    #: 可见工具名（顺序即渲染顺序）。
    tools: tuple[str, ...] = ()
    #: 模型调用次数上限（不是工具轮数）。
    max_iterations: int = 1
    #: 是否把正文增量作为 `TextDelta` 发出去（只有回答/探索型 agent 需要）。
    emit_text: bool = False
    #: 调用工具前补齐内部字段（`_input_data`）。
    enrich_input: EnrichInput | None = None
    #: 工具执行完的旁路钩子（收集计算结果等）。
    on_tool_result: OnToolResult | None = None
    #: 收尾：从累积正文里抽出 agent 的产出（```plan```、探索报告）。
    finish_extract: FinishExtract | None = None


def extract_plan(text: str) -> str:
    """从分析 agent 的输出里抽查询计划。

    优先 ```plan``` 块，退而求其次任意代码块，最后退到正文前 3000 字 ——
    历史行为如此（模型经常忘记带语言标记）。
    """
    import re

    match = re.search(r"```plan\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text[:3000]


# ---------------------------------------------------------------- 工厂


def analysis_spec(
    settings: Settings, dialect: Dialect | None, registry: ToolRegistry
) -> AgentSpec:
    """分析 / 规划 agent。"""
    return AgentSpec(
        name=AGENT_ANALYSIS,
        display="分析规划",
        system_prompt=build_analysis_prompt(dialect),
        tools=registry.for_agent(AGENT_ANALYSIS),
        max_iterations=settings.max_analysis_iterations,
        finish_extract=extract_plan,
    )


async def sql_spec(
    settings: Settings,
    *,
    registry: ToolRegistry,
    dialect: Dialect | None,
    snapshot: SchemaSnapshot | None,
    scope: CatalogScope,
    profile_id: str = "",
    exploration_report: str = "",
    analysis_plan: str = "",
) -> AgentSpec:
    """SQL agent。结构上下文按计划里提到的表聚焦（大库模式的关键）。"""
    focus_tables = extract_table_refs(analysis_plan) if analysis_plan else None
    schema_context = await build_schema_context(
        snapshot,
        scope=scope,
        dialect=dialect,
        focus_tables=focus_tables,
        profile_id=profile_id,
        settings=settings,
    )
    return AgentSpec(
        name=AGENT_SQL,
        display="查询数据",
        system_prompt=build_sql_prompt(
            dialect=dialect,
            schema_context=schema_context,
            exploration_report=exploration_report,
            analysis_plan=analysis_plan,
        ),
        tools=registry.for_agent(AGENT_SQL),
        max_iterations=settings.max_sql_iterations,
    )


def compute_spec(
    settings: Settings,
    *,
    registry: ToolRegistry,
    question: str,
    results: Sequence[QueryResult],
    strategy: str = "",
    enable_charts: bool = False,
) -> AgentSpec:
    """计算 agent（可选带画图）。"""
    input_data = build_query_input_data(results)
    return AgentSpec(
        name=AGENT_COMPUTE,
        display="计算",
        system_prompt=build_compute_prompt(
            question, results, strategy=strategy, enable_charts=enable_charts
        ),
        tools=registry.for_agent(AGENT_COMPUTE),
        max_iterations=settings.max_compute_iterations,
        enrich_input=input_injector(input_data),
        on_tool_result=_record_computed,
    )


def visualize_spec(
    settings: Settings,
    *,
    registry: ToolRegistry,
    question: str,
    results: Sequence[QueryResult],
    chart_strategy: str = "",
) -> AgentSpec:
    """可视化 agent（只画图）。"""
    input_data = build_query_input_data(results)
    return AgentSpec(
        name=AGENT_VISUALIZE,
        display="生成图表",
        system_prompt=build_visualize_prompt(question, results, chart_strategy=chart_strategy),
        tools=registry.for_agent(AGENT_VISUALIZE),
        max_iterations=settings.max_visualization_iterations,
        enrich_input=input_injector(input_data),
        on_tool_result=_record_computed,
    )


def answer_spec(
    settings: Settings,
    *,
    question: str,
    results: Sequence[QueryResult],
    computed: Sequence[str] = (),
    artifacts: Sequence[Artifact] = (),
) -> AgentSpec:
    """最终回答 agent —— 就是"一次调用、没有工具"。"""
    return AgentSpec(
        name=AGENT_ANSWER,
        display="组织回答",
        system_prompt=build_answer_prompt(
            question, results, computed=computed, artifacts=artifacts
        ),
        tools=(),
        max_iterations=1,
        emit_text=True,
    )


async def explore_spec(
    settings: Settings,
    *,
    registry: ToolRegistry,
    dialect: Dialect | None,
    db_name: str,
    save_report: Callable[[str], Awaitable[None]] | None = None,
) -> AgentSpec:
    """探索 agent。

    `finish_extract` 同时承担"抽取产出"和"落盘"两件事：探索报告必须保存，
    否则 `_is_explored()` 语义断掉（会对同一个库反复做全量探索）。
    """

    async def _finish(text: str) -> str:
        report = text
        if save_report is not None:
            await save_report(report)
        return report

    return AgentSpec(
        name=AGENT_EXPLORE,
        display="探索数据库",
        system_prompt=build_explore_prompt(dialect, db_name),
        tools=registry.for_agent(AGENT_EXPLORE),
        max_iterations=settings.max_sql_iterations,
        # 探索报告不进用户可见的回答流（历史实现把它直接流给了用户）。
        emit_text=False,
        finish_extract=_finish,
    )


def _record_computed(tool: str, outcome: ToolOutcome, run: AgentRun) -> None:
    """把 `run_python` 的输出记进旁路状态（供回答 agent 使用）。"""
    if tool == "run_python" and outcome.content:
        run.record_computed(outcome.content)


def spec_tools(spec: AgentSpec, registry: ToolRegistry) -> tuple[str, ...]:
    """校验 spec 里的工具都在注册表里（测试与调试用）。"""
    return tuple(registry.require(name).name for name in spec.tools)


def build_specs(
    settings: Settings | None = None,
    *,
    registry: ToolRegistry | None = None,
    dialect: Dialect | None = None,
) -> dict[str, AgentSpec]:
    """一次性构造所有"非会话相关"的 spec（供测试与文档使用）。

    `sql` / `answer` / `explore` 依赖本次提问的数据（问题、快照、结果），
    由用例层按需构造。
    """
    from dbchat.config import get_settings
    from dbchat.tools import get_registry

    active_settings = settings or get_settings()
    active_registry = registry or get_registry()
    return {
        AGENT_ANALYSIS: analysis_spec(active_settings, dialect, active_registry),
        AGENT_COMPUTE: compute_spec(
            active_settings, registry=active_registry, question="", results=()
        ),
        AGENT_VISUALIZE: visualize_spec(
            active_settings, registry=active_registry, question="", results=()
        ),
        AGENT_ANSWER: answer_spec(active_settings, question="", results=()),
    }
