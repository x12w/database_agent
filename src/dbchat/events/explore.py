"""探索通道的事件契约（管理面板用）。

与聊天通道分开，因为两者的读者不同：聊天事件的读者是提问的普通用户，
不该看到表名列名；探索事件的读者是管理员，**必须**看到表名、列名、
样本值和 agent 的推测 —— 否则无从判断该怎么回答。

历史实现里这条通道的事件名（`progress` / `admin_question` / `done` /
`explore_error`）从未被契约化，前端只能照抄旧 JS 的字符串。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dbchat.events.base import ErrorCode, EventBase, ProfileView


class ExploreStarted(EventBase):
    """一次全库探索开始。"""

    type: Literal["explore.started"] = "explore.started"
    profile: ProfileView
    #: 本次计划检查的候选列数（含义不明的编码列）。已确认过的列不计入。
    candidates: int = 0


class ExploreProgress(EventBase):
    """探索进度。

    大库探索可能持续几分钟，必须让管理员看到推进情况。
    """

    type: Literal["explore.progress"] = "explore.progress"
    done: int
    total: int
    #: 当前正在检查的表（管理员视角，可见）。
    current_table: str | None = None
    note: str | None = None

    @property
    def ratio(self) -> float:
        return (self.done / self.total) if self.total else 0.0


class ExploreQuestion(EventBase):
    """探索发现一个含义不明的列，向管理员提问。"""

    type: Literal["explore.question"] = "explore.question"
    question_id: str
    table: str
    column: str
    question: str
    #: agent 的推测（可能是错的，管理员据此判断是"同意"还是"修正"）。
    agent_guess: str = ""
    #: 用于提供判断依据的样本值。
    samples: list[str] = Field(default_factory=list)
    #: 该列在表中的样例上下文（若干行的相关列取值）。
    context_columns: list[str] = Field(default_factory=list)
    context_rows: list[list[str]] = Field(default_factory=list)
    #: 历史提问去重命中时，携带已有答案而非重复提问。
    existing_answer: str | None = None


class ExploreFinished(EventBase):
    """探索结束。"""

    type: Literal["explore.finished"] = "explore.finished"
    candidates_found: int = 0
    questions_asked: int = 0
    #: 仍待管理员回答的数量 —— 大于 0 时不应显示"完成"，
    #: 而应显示"等待管理员回答"。
    pending_count: int = 0
    elapsed_ms: int = 0


class ExploreFailed(EventBase):
    """探索失败。"""

    type: Literal["explore.failed"] = "explore.failed"
    code: ErrorCode
    message: str
    error_detail: str | None = None


#: 探索通道事件联合。
ExploreEvent = Annotated[
    ExploreStarted | ExploreProgress | ExploreQuestion | ExploreFinished | ExploreFailed,
    Field(discriminator="type"),
]

EXPLORE_EVENT_TYPES: tuple[str, ...] = (
    "explore.started",
    "explore.progress",
    "explore.question",
    "explore.finished",
    "explore.failed",
)

__all__ = [
    "EXPLORE_EVENT_TYPES",
    "ExploreEvent",
    "ExploreFailed",
    "ExploreFinished",
    "ExploreProgress",
    "ExploreQuestion",
    "ExploreStarted",
]
