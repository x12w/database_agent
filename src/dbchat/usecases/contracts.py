"""用例层的边界接口。

API 层只依赖这里定义的类型，不直接接触 agent / tools / db 的内部对象。
这样 API 层与 agent 运行时可以并行开发，也让将来换掉编排方式时
只影响这一个接缝。

`RunContext` 是一次提问的全部可变状态。历史实现把这些散在模块级全局里
（`db` 单例、两个 `_active_table_filter`），导致并发请求互相串扰。
这里全部收进一个显式对象，随调用链传递。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.query import Artifact, QueryResult
from dbchat.domain.schema import CatalogScope, SchemaSnapshot
from dbchat.events.agent import AgentEvent
from dbchat.events.chat import ChatEvent


@dataclass(slots=True)
class RunContext:
    """一次提问的可变状态。

    生命周期 = 一个 HTTP 请求。请求结束即释放，不跨请求共享。
    """

    run_id: str
    profile: DatabaseProfile
    scope: CatalogScope
    conversation_id: str
    #: 数据库结构快照（可能来自缓存）。
    snapshot: SchemaSnapshot | None = None
    #: 本次运行累积的查询结果 —— 供计算、图表与最终回答使用。
    query_results: list[QueryResult] = field(default_factory=list)
    #: 计算沙箱产出的中间结果。
    computed_results: list[QueryResult] = field(default_factory=list)
    #: 图表等产物。
    artifacts: list[Artifact] = field(default_factory=list)
    #: 历史消息（供多轮对话）。
    history: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    #: 本次运行实际执行的数据库查询次数。
    #:
    #: 历史实现里这个数字恒为 0：`orchestrator.py:354` 判断的
    #: `event["type"] == "tool_use"` 是 LLM 客户端层的事件名，
    #: 而 agent 层投递的是 `tool_call`，条件永不成立。界面向用户
    #: 展示的"共 N 次查询"因此一直是错的。这里由工具层按工具名直接计数。
    query_count: int = 0
    #: 本次运行是否真的执行过查询 —— 供结果评估区分
    #: "没查"（该去查）与"查了但为空"（该换口径）。
    ran_any_query: bool = False

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def add_query_result(self, result: QueryResult) -> None:
        self.query_results.append(result)
        self.query_count += 1
        self.ran_any_query = True

    @property
    def total_rows(self) -> int:
        return sum(r.row_count for r in self.query_results)


class AgentEventSink(Protocol):
    """agent 层事件的接收端。

    投影与序列化都在实现方完成 —— agent 层只负责产生内部事件，
    不需要知道 SSE、JSON 或前端的存在。
    """

    async def emit(self, event: AgentEvent) -> None: ...


class ChatUseCase(Protocol):
    """聊天主管道。

    产出**线上事件**（已投影、已脱敏），API 层直接序列化即可。
    """

    def run(
        self,
        *,
        message: str,
        ctx: RunContext,
    ) -> AsyncIterator[ChatEvent]: ...


class ExploreUseCase(Protocol):
    """主动探索（管理面板）。

    产出探索通道事件；探索通道的读者是管理员，
    表名、列名、样本值对他们是必要信息。
    """

    def run(
        self,
        *,
        profile: DatabaseProfile,
        scope: CatalogScope,
    ) -> AsyncIterator[object]: ...


__all__ = [
    "AgentEventSink",
    "ChatUseCase",
    "ExploreUseCase",
    "RunContext",
]
