"""agent 层 —— 唯一的循环 + 各 agent 的配置。

对外只需要认识四样东西：

* `run_agent_loop` —— 唯一的 agent 循环（历史实现里有七份副本）；
* `AgentSpec` / `agent.specs` —— 六个 agent 的差异只是配置；
* `classify` —— 问题分类（协程，由管道 await）；
* `deciders` —— 结果评估与计算规划（都是 `LLMClient.complete`，不阻塞事件循环）。
"""

from dbchat.agent.classifier import classify
from dbchat.agent.deciders import ComputationPlan, Evaluation, evaluate_results, plan_computation
from dbchat.agent.history import build_messages, strip_sql_from_text, trim_history
from dbchat.agent.inputs import build_query_input_data, input_injector
from dbchat.agent.loop import run_agent_loop
from dbchat.agent.prompts import (
    build_analysis_history_block,
    build_analysis_user_message,
    build_explore_prompt,
    build_nl_history,
    build_schema_context,
)
from dbchat.agent.run import AgentRun
from dbchat.agent.specs import (
    AgentSpec,
    analysis_spec,
    answer_spec,
    compute_spec,
    explore_spec,
    extract_plan,
    sql_spec,
    visualize_spec,
)
from dbchat.agent.structure import (
    build_structure_response,
    is_simple_query,
    is_structure_question,
)
from dbchat.agent.temporal import TemporalContext, resolve_temporal_context

__all__ = [
    "AgentRun",
    "build_analysis_history_block",
    "build_analysis_user_message",
    "build_explore_prompt",
    "build_nl_history",
    "build_schema_context",
    "AgentSpec",
    "ComputationPlan",
    "Evaluation",
    "TemporalContext",
    "analysis_spec",
    "answer_spec",
    "build_messages",
    "build_query_input_data",
    "build_structure_response",
    "classify",
    "compute_spec",
    "evaluate_results",
    "explore_spec",
    "extract_plan",
    "input_injector",
    "is_simple_query",
    "is_structure_question",
    "plan_computation",
    "resolve_temporal_context",
    "run_agent_loop",
    "sql_spec",
    "strip_sql_from_text",
    "trim_history",
    "visualize_spec",
]
