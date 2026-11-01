"""工具注册表 —— 工具定义的**唯一来源**。

历史实现（`app/agent/tools.py`）把同一批 14 个工具定义了两遍（Anthropic 形状
`TOOLS` 与 OpenAI 形状 `OPENAI_TOOLS`），又按 provider 各写了一份"位置下标
列表"（`SQL_AGENT_TOOLS = [TOOLS[0], TOOLS[5], ...]`），最后在 `get_tools()`
里维护两张平行字典。改一个工具的可见范围要同步改四处，漏一处的表现是
"某个 provider 上工具凭空消失"。

这里每个工具只有一条记录：

* `params` 是 **pydantic 模型**，JSON Schema 由它推导 —— 定义与校验同源，
  模型给出的非法入参在执行前就被挡住并回一条可自我纠正的错误；
* `handler` 是 **async** 函数 —— 历史 `execute_tool` 是一条 180 行的同步
  `if/elif` 链，直接在事件循环上跑同步 DB/网络/嵌入代码，一个查询就能把
  整条 SSE 流卡住；
* `agents` 是可见范围，`get_tools(name)` 之类的调用点全部消失。

工具名到线上事件的映射在 `dbchat.events.project.TOOL_VIEW`，
`tests/unit/test_specs.py` 会强制两边一致。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from dbchat.config import Settings
from dbchat.db.dialects import get_dialect
from dbchat.db.dialects.base import Dialect
from dbchat.db.executor import SqlExecutor
from dbchat.domain.errors import DbError, ToolError
from dbchat.domain.query import Artifact
from dbchat.events.agent import AgentEvent
from dbchat.llm.client import LLMClient
from dbchat.llm.events import CancelToken
from dbchat.llm.messages import to_anthropic_tools, to_openai_tools

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    # `usecases.contracts` 是**边界模块**（只放类型），不是上层实现；
    # 工具需要按名计数（见 `RunContext.query_count`），所以这里引用它。
    from dbchat.usecases.contracts import RunContext

__all__ = [
    "AGENT_NAMES",
    "AGENT_ANALYSIS",
    "AGENT_ANSWER",
    "AGENT_COMPUTE",
    "AGENT_EXPLORE",
    "AGENT_SQL",
    "AGENT_VISUALIZE",
    "MAX_PARALLEL_TOOL_CALLS",
    "ToolContext",
    "ToolHandler",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "strip_internal_keys",
]

logger = logging.getLogger(__name__)

#: agent 名字词汇表 —— 工具可见范围与 `AgentSpec.name` 共用同一套取值。
#:
#: 历史实现用 `get_tools("sql_agent")` 这样的字符串散在各处，拼错只会静默
#: 退回默认工具集（`mapping.get(classification, mapping["AMBIGUOUS"])`）。
AGENT_ANALYSIS = "analysis"
AGENT_SQL = "sql"
AGENT_COMPUTE = "compute"
AGENT_VISUALIZE = "visualize"
AGENT_ANSWER = "answer"
AGENT_EXPLORE = "explore"

AGENT_NAMES: frozenset[str] = frozenset(
    {AGENT_ANALYSIS, AGENT_SQL, AGENT_COMPUTE, AGENT_VISUALIZE, AGENT_ANSWER, AGENT_EXPLORE}
)

#: 一轮里同时执行的工具调用上限。
#:
#: 工具调用是 IO 密集（DB + HTTP），串行执行会把延迟直接相加；但也不能
#: 无限并发 —— 同一条 DB 连接本来就不能并发使用，网络请求也不该一次打出去
#: 十几个。4 是"能重叠等待、又不会打满连接/网络"的折中值。
MAX_PARALLEL_TOOL_CALLS = 4

#: 内部字段前缀。带这个前缀的入参**只**给工具自己用（如注入的 `_input_data`），
#: 不得进入 `ToolStarted` 事件，也不得写进对话历史。
INTERNAL_KEY_PREFIX = "_"


def strip_internal_keys(tool_input: Mapping[str, Any]) -> dict[str, Any]:
    """剥掉 `_` 前缀的内部字段。

    投影层依赖这个保证：`ToolStarted.tool_input` 里不该出现 `_input_data`
    这种又大又无意义的内部载荷。
    """
    return {k: v for k, v in tool_input.items() if not k.startswith(INTERNAL_KEY_PREFIX)}


class ToolHandler:
    """工具实现的调用形状（仅用于文档与类型提示）。"""

    def __call__(self, params: Any, ctx: ToolContext) -> Awaitable[ToolOutcome]:
        raise NotImplementedError


def static_description(text: str) -> Callable[[Dialect | None], str]:
    """把静态描述包成"按方言取值"的描述函数。"""
    return lambda _dialect: text


@dataclass(slots=True, frozen=True)
class ToolSpec:
    """一个工具的完整定义。"""

    name: str
    #: 用户可见的名字（`ToolFinished.summary` 之外的展示文案用得到）。
    label: str
    #: 入参模型 —— JSON Schema 由它推导，同时也是运行时校验。
    params: type[BaseModel]
    handler: Callable[[Any, ToolContext], Awaitable[ToolOutcome]]
    description: Callable[[Dialect | None], str]
    #: 该工具对哪些 agent 可见（见 `dbchat.agent.specs`）。
    agents: frozenset[str]
    #: 单次调用的超时（秒），由 `Settings` 派生，不写死。
    timeout: Callable[[Settings], float]
    #: 是否需要在数据库上执行语句（决定编排层是否必须先拿到连接）。
    needs_database: bool = False

    def definition(self, dialect: Dialect | None = None) -> dict[str, Any]:
        """渲染成 Anthropic 形状的工具定义（`llm.messages` 的规范形态）。"""
        return {
            "name": self.name,
            "description": self.description(dialect),
            "input_schema": json_schema(self.params),
        }

    def timeout_for(self, settings: Settings) -> float:
        return float(self.timeout(settings))

    def parse(self, raw: Mapping[str, Any] | None) -> BaseModel:
        """校验入参。

        Raises:
            ToolError: 校验未通过 —— 错误文本会原样回给模型，让它自我纠正。
        """
        payload = dict(raw or {})
        try:
            return self.params.model_validate(payload)
        except ValidationError as exc:
            raise ToolError(
                f"{self.name} 的入参不合法：{_summarize_validation(exc)}",
                detail=str(exc),
                cause=exc,
            ) from exc


@dataclass(slots=True)
class ToolOutcome:
    """一次工具调用的结果。

    `content` 给模型看（进对话历史），`summary` 给用户看（进线上事件），
    `payload` 是原始结果（含 SQL、行数据），**只**在进程内流动。
    """

    ok: bool
    summary: str
    content: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    #: 工具顺带产生的 agent 事件（目前只有 `AdminQuestionRaised`）。
    events: tuple[AgentEvent, ...] = ()
    error_detail: str = ""

    @classmethod
    def failure(cls, summary: str, *, content: str = "", detail: str = "") -> ToolOutcome:
        """构造一个失败结果 —— 失败也必须是一条**结果**，不是异常。"""
        return cls(
            ok=False,
            summary=summary,
            content=content or summary,
            payload={"error_detail": detail or summary},
            error_detail=detail or summary,
        )


@dataclass(slots=True)
class ToolContext:
    """工具执行所需的一切（一次运行一个）。

    刻意不持有任何全局状态：历史实现的 `_active_table_filter` 是模块级
    可变量，两个并发请求会互相改对方的可见表范围。

    连接是**按需借还**的（`database()`），不在整次提问期间长期占用：同一个
    profile 的连接由 `ConnectionManager` 串行化，一次两分钟的提问若一直握着
    租约，别人查同一个库就得等两分钟。
    """

    ctx: RunContext
    settings: Settings
    #: 显式注入的执行器（测试、或调用方已经持有租约时）。留空则按需借。
    executor: SqlExecutor | None = None
    llm: LLMClient | None = None
    cancel: CancelToken | None = None
    #: 管理员问答仓储。API 层自己持有一份（`AppContext.clarifications`），
    #: 注入进来可以让工具与路由读写同一个对象，而不是各自开一条 SQLite 连接。
    clarifications: Any | None = None
    rag: Any | None = None

    @property
    def profile_id(self) -> str:
        return self.ctx.profile.id

    @property
    def scope(self) -> Any:
        return self.ctx.scope

    @property
    def snapshot(self) -> Any:
        return self.ctx.snapshot

    @property
    def dialect(self) -> Dialect:
        """当前方言（由连接配置决定，不依赖是否已借到连接）。"""
        return get_dialect(self.ctx.profile.kind)

    @asynccontextmanager
    async def database(self) -> AsyncIterator[SqlExecutor]:
        """借一条连接执行若干语句，用完立刻归还。"""
        if self.executor is not None:
            yield self.executor
            return
        from dbchat.db.connection import get_connection_manager

        async with get_connection_manager().acquire(self.ctx.profile) as lease:
            yield SqlExecutor(lease, self.settings)


class ToolRegistry:
    """工具名 → 工具定义。"""

    def __init__(
        self,
        specs: Sequence[ToolSpec],
        *,
        max_parallel: int = MAX_PARALLEL_TOOL_CALLS,
        settings: Settings | None = None,
    ) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError(f"工具名重复注册: {spec.name}")
            self._specs[spec.name] = spec
        self.max_parallel = max(1, int(max_parallel))
        self._settings = settings

    # ---- 查询 ----

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> ToolSpec:
        """取工具定义。

        Raises:
            ToolError: 未注册的工具名 —— 模型臆造工具名时必须得到明确反馈。
        """
        spec = self._specs.get(name)
        if spec is None:
            available = ", ".join(sorted(self._specs))
            raise ToolError(f"未知工具 {name!r}；可用工具: {available}")
        return spec

    def for_agent(self, agent: str) -> tuple[str, ...]:
        """某 agent 可见的工具名（保持注册顺序）。"""
        return tuple(name for name, spec in self._specs.items() if agent in spec.agents)

    def definitions(
        self,
        names: Sequence[str],
        dialect: Dialect | None = None,
        *,
        provider: str = "anthropic",
    ) -> list[dict[str, Any]]:
        """把工具定义渲染成指定 provider 的线格式。

        `provider` 只影响渲染形状；`LLMClient.stream` 接受的是 Anthropic 形状
        （它内部再转换），所以调用方通常不传这个参数。
        """
        specs = [self.require(name).definition(dialect) for name in names]
        if provider == "openai":
            return to_openai_tools(specs)
        return to_anthropic_tools(specs)

    # ---- 执行 ----

    async def run(
        self,
        name: str,
        tool_input: Mapping[str, Any],
        ctx: ToolContext,
    ) -> ToolOutcome:
        """执行一个工具调用。

        **任何**异常都会变成失败结果：工具炸掉不该让整次提问崩掉，但必须
        留下细节（`error_detail`）供排查，不能静默。未知工具名同样是失败
        结果 —— 模型下一轮能据此改口。
        """
        try:
            spec = self.require(name)
        except ToolError as exc:
            return ToolOutcome.failure(exc.user_message, detail=exc.detail)

        try:
            params = spec.parse(tool_input)
        except ToolError as exc:
            return ToolOutcome.failure(exc.user_message, detail=exc.detail)

        try:
            return await spec.handler(params, ctx)
        except DbError as exc:
            logger.info("工具 %s 执行失败（数据库）: %s", name, exc.detail or exc.user_message)
            return ToolOutcome.failure(exc.user_message, detail=exc.detail)
        except TimeoutError as exc:
            logger.warning("工具 %s 执行超时: %s", name, exc)
            return ToolOutcome.failure(f"{spec.label}超时", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - 工具失败必须变成结果并留痕
            logger.warning("工具 %s 执行出错: %s: %s", name, type(exc).__name__, exc, exc_info=True)
            return ToolOutcome.failure(
                f"{spec.label}执行出错",
                detail=f"{type(exc).__name__}: {exc}",
            )


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """由 pydantic 模型推导 JSON Schema。

    `title` 是 pydantic 的产物，对模型理解 schema 没有帮助，只会白占
    token；递归剔除后两个 provider 看到的定义也更一致。
    """
    return _strip_titles(model.model_json_schema())


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    return node


def _summarize_validation(exc: ValidationError) -> str:
    """把 pydantic 报错压成一行，便于模型自我纠正。"""
    parts: list[str] = []
    for error in exc.errors()[:4]:
        location = ".".join(str(piece) for piece in error.get("loc", ())) or "(根)"
        parts.append(f"{location}: {error.get('msg', '')}")
    return "; ".join(parts) or "参数格式不正确"


def db_timeout(settings: Settings) -> float:
    """数据库类工具的超时：查询超时 + 一点余量给驱动层。"""
    return float(settings.db_query_timeout_s) + 5.0


def llm_timeout(settings: Settings) -> float:
    """需要调模型或嵌入编码的工具超时。"""
    return float(settings.llm_timeout_s)


def sandbox_timeout(settings: Settings) -> float:
    """计算沙箱超时。"""
    return float(settings.sandbox_timeout_s)


def search_timeout(settings: Settings) -> float:
    """外网检索超时（比查询短得多 —— 搜不到就换关键词）。"""
    return min(30.0, float(settings.db_query_timeout_s))
