"""线上事件契约 —— 浏览器能看到的全部内容。

这里**刻意不包含**：原始 SQL、工具内部名、问题分类枚举、轮次上限、
驱动原始报文。要加这类信息，必须修改本文件（显式且可 review），
而不是在某个 handler 里顺手塞进响应。

字段命名用点号分段（`run.started`）而非历史实现里的 `error`：
`error` 是浏览器的保留 SSE 事件名，旧代码不得不在传输层把它改名成
`explore_error`（main.py:684），点号命名从根上消除了这个 hack。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dbchat.events.base import (
    ActivityKind,
    ErrorCode,
    EventBase,
    ProfileView,
    RunStats,
    Stage,
    StageView,
)


class RunStarted(EventBase):
    """一次提问开始处理。"""

    type: Literal["run.started"] = "run.started"
    profile: ProfileView
    stages: list[StageView] = Field(default_factory=list)
    #: 是否会先加载/重建数据库结构（长耗时操作，需要先告诉用户）。
    will_load_schema: bool = False


class StageStarted(EventBase):
    """进入某个阶段。"""

    type: Literal["stage.started"] = "stage.started"
    stage: Stage
    label: str
    index: int
    total: int
    detail: str | None = None


class StageFinished(EventBase):
    """阶段结束。"""

    type: Literal["stage.finished"] = "stage.finished"
    stage: Stage
    ok: bool
    summary: str
    elapsed_ms: int = 0


class StageRetried(EventBase):
    """阶段因结果不充分而重试。

    只在真的重试时发出 —— 取代历史上每次都显示"最多 5 轮迭代"的做法。
    """

    type: Literal["stage.retried"] = "stage.retried"
    stage: Stage
    attempt: int
    max_attempts: int
    reason: str


class ActivityStarted(EventBase):
    """一个具体动作开始（查询、检索、计算……）。"""

    type: Literal["activity.started"] = "activity.started"
    activity_id: str
    kind: ActivityKind
    label: str
    #: 目标对象的**名称**（如涉及的表名），绝不是 SQL 文本。
    target: str | None = None


class ActivityFinished(EventBase):
    """动作结束。"""

    type: Literal["activity.finished"] = "activity.finished"
    activity_id: str
    ok: bool
    #: 一句话结果摘要，如"返回 128 行"。
    summary: str
    row_count: int | None = None
    elapsed_ms: int | None = None
    #: 仅当配置允许向用户展示 SQL 时才有值（默认关闭）。
    sql_preview: str | None = None
    #: 失败时的详细信息（原始报错），默认收起，仅用户主动展开时可见。
    error_detail: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)


class ThoughtDelta(EventBase):
    """模型的思考过程增量文本。

    用户明确要求"把过程文本展示出来"，所以这里保留原文；
    前端以暗淡样式流式渲染，并在步骤结束后折叠。
    """

    type: Literal["thought.delta"] = "thought.delta"
    text: str


class AnswerDelta(EventBase):
    """最终回答的流式增量。"""

    type: Literal["answer.delta"] = "answer.delta"
    text: str


class AnswerReset(EventBase):
    """丢弃已流出的回答片段，用新内容重新开始。

    重试轮次里上一轮的回答需要作废，否则用户会看到两段互相矛盾的答案。
    """

    type: Literal["answer.reset"] = "answer.reset"


class ArtifactCreated(EventBase):
    """产生了可展示的产物（图表等）。"""

    type: Literal["artifact.created"] = "artifact.created"
    artifact_id: str
    kind: Literal["chart", "table", "file"]
    url: str
    title: str = ""


class QuestionAsked(EventBase):
    """agent 需要用户确认一个业务含义（管理员问答闭环）。"""

    type: Literal["question.asked"] = "question.asked"
    question_id: str
    label: str
    table: str
    column: str
    samples: list[str] = Field(default_factory=list)


class RunFinished(EventBase):
    """正常结束。"""

    type: Literal["run.finished"] = "run.finished"
    status: Literal["ok", "partial"] = "ok"
    elapsed_ms: int = 0
    stats: RunStats = Field(default_factory=RunStats)
    #: 完整回答（前端在流式结束后用它做 markdown 渲染）。
    answer_markdown: str = ""
    #: 本次未能回答的部分（如部分子问题失败）。
    caveats: list[str] = Field(default_factory=list)


class RunFailed(EventBase):
    """异常结束。"""

    type: Literal["run.failed"] = "run.failed"
    code: ErrorCode
    #: 面向用户的中文文案（来自 ErrorCode 与异常类型，不是驱动报文）。
    message: str
    retryable: bool = False
    #: 原始细节，仅日志与调试模式使用。
    error_detail: str | None = None


#: 线上事件联合类型。前端按 `type` 做穷尽分发。
ChatEvent = Annotated[
    RunStarted
    | StageStarted
    | StageFinished
    | StageRetried
    | ActivityStarted
    | ActivityFinished
    | ThoughtDelta
    | AnswerDelta
    | AnswerReset
    | ArtifactCreated
    | QuestionAsked
    | RunFinished
    | RunFailed,
    Field(discriminator="type"),
]

#: 所有线上事件类型的名字，供 codegen 与测试使用。
CHAT_EVENT_TYPES: tuple[str, ...] = (
    "run.started",
    "stage.started",
    "stage.finished",
    "stage.retried",
    "activity.started",
    "activity.finished",
    "thought.delta",
    "answer.delta",
    "answer.reset",
    "artifact.created",
    "question.asked",
    "run.finished",
    "run.failed",
)

__all__ = [
    "CHAT_EVENT_TYPES",
    "ActivityFinished",
    "ActivityStarted",
    "AnswerDelta",
    "AnswerReset",
    "ArtifactCreated",
    "ChatEvent",
    "QuestionAsked",
    "RunFailed",
    "RunFinished",
    "RunStarted",
    "StageFinished",
    "StageRetried",
    "StageStarted",
    "ThoughtDelta",
]
