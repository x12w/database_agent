"""事件模型基础类型。

事件分两层，这是本项目的核心约束：

* `dbchat.events.agent.AgentEvent` —— **内部**事件。可以携带原始 SQL、
  工具名、行数据、驱动报文。只在进程内流动。
* `dbchat.events.chat.ChatEvent` —— **线上**事件。唯一会序列化给浏览器的类型。
  它的字段里**不存在**原始 SQL、工具名、分类枚举这类内部信息。

两层之间只能通过 `dbchat.events.project.project()` 转换。想泄漏内部信息，
必须往 ChatEvent 上加字段 —— 那是一个显式、可 review 的动作，
而不是随手把一个 dict 塞进响应里。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Stage(StrEnum):
    """管道阶段。用户看到的是 STAGE_LABELS 里的中文，不是这些值。"""

    UNDERSTANDING = "understanding"  # 理解问题 / 探索数据
    QUERYING = "querying"            # 生成并执行查询
    COMPUTING = "computing"          # 计算
    VISUALIZING = "visualizing"      # 生成图表
    ANSWERING = "answering"          # 组织回答


#: 阶段 → 用户可见文案。前端只显示这个，不显示 Stage 的原始值。
STAGE_LABELS: dict[Stage, str] = {
    Stage.UNDERSTANDING: "理解问题",
    Stage.QUERYING: "查询数据",
    Stage.COMPUTING: "进行计算",
    Stage.VISUALIZING: "生成图表",
    Stage.ANSWERING: "组织回答",
}


class ActivityKind(StrEnum):
    """用户可见的动作类别。前端据此选图标。"""

    QUERY = "query"
    SCHEMA = "schema"
    SEARCH = "search"
    DOCS = "docs"
    COMPUTE = "compute"
    CHART = "chart"
    ADMIN_ASK = "admin_ask"


#: 动作类别 → 进行中的用户可见文案。
ACTIVITY_LABELS: dict[ActivityKind, str] = {
    ActivityKind.QUERY: "正在查询数据",
    ActivityKind.SCHEMA: "正在查看表结构",
    ActivityKind.SEARCH: "正在检索资料",
    ActivityKind.DOCS: "正在查证文档",
    ActivityKind.COMPUTE: "正在计算",
    ActivityKind.CHART: "正在生成图表",
    ActivityKind.ADMIN_ASK: "正在向你确认",
}


class EventBase(BaseModel):
    """所有线上事件的公共字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 本次运行的单调递增序号，供前端检测丢帧。
    seq: int
    #: Unix 时间戳（秒）。
    ts: float
    #: 同一次提问的标识。
    run_id: str


class RunStats(BaseModel):
    """一次运行的统计数字。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    queries: int = 0
    rows: int = 0
    charts: int = 0


class StageView(BaseModel):
    """运行开始时告知前端本次计划经过哪些阶段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Stage
    label: str


class ProfileView(BaseModel):
    """连接信息 —— 只含用户自己填的展示名，不含密码与主机细节。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    dialect_label: str


class ErrorCode(StrEnum):
    """线上错误码。前端据此决定文案与"是否可重试"的提示。"""

    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_FAILED = "llm_failed"
    DB_UNREACHABLE = "db_unreachable"
    DB_AUTH = "db_auth"
    DB_TIMEOUT = "db_timeout"
    DB_QUERY_FAILED = "db_query_failed"
    VALIDATION = "validation"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


#: 错误码 → 是否值得提示用户重试。
RETRYABLE_ERRORS: frozenset[ErrorCode] = frozenset(
    {ErrorCode.LLM_FAILED, ErrorCode.DB_TIMEOUT, ErrorCode.TIMEOUT, ErrorCode.INTERNAL}
)


class Timings(BaseModel):
    """可选耗时信息。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    elapsed_ms: int = 0


__all__ = [
    "ACTIVITY_LABELS",
    "RETRYABLE_ERRORS",
    "STAGE_LABELS",
    "ActivityKind",
    "ErrorCode",
    "EventBase",
    "ProfileView",
    "RunStats",
    "Stage",
    "StageView",
    "Timings",
]


_ = Field  # re-exported for sibling modules
