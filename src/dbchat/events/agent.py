"""内部事件 —— agent 层的输出，**不会**直接序列化给浏览器。

允许携带原始 SQL、工具内部名、行数据、驱动报文。
只有 `dbchat.events.project` 能把它们转换成线上事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dbchat.domain.query import Artifact


@dataclass(slots=True, frozen=True)
class ThinkingDelta:
    """模型思考文本增量。"""

    agent: str
    text: str


@dataclass(slots=True, frozen=True)
class TextDelta:
    """模型正文增量（仅回答型 agent 会产生）。"""

    agent: str
    text: str


@dataclass(slots=True, frozen=True)
class ToolStarted:
    """工具调用开始。

    `tool_input` 是**已剥离**内部字段（`_` 前缀，如 `_input_data`）的入参，
    可以安全地用于生成展示文案。
    """

    invocation_id: str
    tool: str
    tool_input: dict[str, Any]


@dataclass(slots=True, frozen=True)
class ToolFinished:
    """工具调用结束。

    `payload` 是原始结果（含 SQL、行数据），**内部专用**：
    它不作为任何线上事件的字段存在。
    """

    invocation_id: str
    tool: str
    ok: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    error_detail: str = ""


@dataclass(slots=True, frozen=True)
class AdminQuestionRaised:
    """agent 通过 ask_admin 提出了一个待确认问题。"""

    question_id: str
    table: str
    column: str
    question: str
    samples: tuple[str, ...] = ()
    #: 该问题是否已有答案（去重命中时直接返回历史答案，不重复提问）。
    already_answered: bool = False


@dataclass(slots=True, frozen=True)
class AgentFinished:
    """一个 agent 的正常结束。"""

    agent: str
    text: str


@dataclass(slots=True, frozen=True)
class AgentFailed:
    """一个 agent 的异常结束。"""

    agent: str
    code: str
    message: str
    detail: str = ""


#: agent 层事件联合。
AgentEvent = (
    ThinkingDelta
    | TextDelta
    | ToolStarted
    | ToolFinished
    | AdminQuestionRaised
    | AgentFinished
    | AgentFailed
)

__all__ = [
    "AdminQuestionRaised",
    "AgentEvent",
    "AgentFailed",
    "AgentFinished",
    "TextDelta",
    "ThinkingDelta",
    "ToolFinished",
    "ToolStarted",
]
