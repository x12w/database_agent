"""一次 agent 调用的运行时上下文。

`RunContext`（`dbchat.usecases.contracts`）是**冻结契约**：它只描述"一次提问
的可变状态"，不含 LLM 客户端、工具注册表、连接这类基础设施。agent 循环需要
这些，所以这里把它们与 `RunContext` 一起打包成 `AgentRun`。

`state` 是给各 agent 的旁路返回值：`run_agent_loop` 是异步生成器（Python 的
异步生成器不能 `return` 值），所以 compute/visualize 收集的计算结果通过
`state` 交回调用方，而不是塞进事件流。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from dbchat.config import Settings
from dbchat.db.dialects.base import Dialect
from dbchat.db.executor import SqlExecutor
from dbchat.llm.client import LLMClient
from dbchat.llm.events import CancelToken
from dbchat.tools.registry import ToolContext, ToolOutcome, ToolRegistry

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from dbchat.usecases.contracts import RunContext

__all__ = ["AgentRun", "STATE_COMPUTED", "STATE_TOOL_OUTCOMES"]

logger = logging.getLogger(__name__)

#: `AgentRun.state` 的键：计算 agent 产出的结果文本列表。
STATE_COMPUTED = "computed"
#: `AgentRun.state` 的键：本轮所有工具结果（按调用顺序）。
STATE_TOOL_OUTCOMES = "tool_outcomes"


@dataclass(slots=True)
class AgentRun:
    """一次 agent 调用的运行时。"""

    ctx: RunContext
    llm: LLMClient
    registry: ToolRegistry
    settings: Settings
    #: 本次运行独占的连接门面（不需要数据库时为 None）。
    executor: SqlExecutor | None = None
    cancel: CancelToken | None = None
    #: 管理员问答仓储（见 `ToolContext.clarifications`）。
    clarifications: Any | None = None
    rag: Any | None = None
    #: agent 之间的旁路数据（见模块 docstring）。
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def dialect(self) -> Dialect | None:
        return self.executor.dialect if self.executor is not None else None

    def tool_context(self) -> ToolContext:
        """构造工具执行上下文（每次调用新建，内部无状态）。"""
        return ToolContext(
            ctx=self.ctx,
            settings=self.settings,
            executor=self.executor,
            llm=self.llm,
            cancel=self.cancel,
            clarifications=self.clarifications,
            rag=self.rag,
        )

    def record_tool(self, tool: str, outcome: ToolOutcome) -> None:
        """收集一次工具结果（供用例层读取，不进入事件流）。"""
        self.state.setdefault(STATE_TOOL_OUTCOMES, []).append((tool, outcome))
        if outcome.artifacts:
            self.add_artifacts(outcome.artifacts)

    def add_artifacts(self, artifacts: tuple) -> None:
        """把产物登记到本次运行（按 artifact_id 去重）。"""
        known = {artifact.artifact_id for artifact in self.ctx.artifacts}
        for artifact in artifacts:
            if artifact.artifact_id in known:
                continue
            known.add(artifact.artifact_id)
            self.ctx.artifacts.append(artifact)

    def record_computed(self, content: str) -> None:
        """记录一段计算结果（compute / visualize agent 用）。"""
        self.state.setdefault(STATE_COMPUTED, []).append(content)

    @property
    def computed(self) -> list[str]:
        return list(self.state.get(STATE_COMPUTED, ()))


def parse_tool_input(raw: Any) -> dict[str, Any]:
    """把模型给出的工具入参规整成 dict（非 dict 一律当空处理）。"""
    if isinstance(raw, BaseModel):
        return raw.model_dump()
    if isinstance(raw, dict):
        return dict(raw)
    return {}
