"""LLM 流式事件的统一词汇表。

两种 provider 的原始事件形状完全不同（Anthropic 是 content_block_*，
OpenAI 是 chat.completion.chunk），上层不应感知这些差异。这里把它们
收敛成五个类型化事件，`llm/providers/*` 负责翻译，`llm/client.py` 负责派发。

历史实现用 `{"type": ...}` 裸 dict 传这些事件，拼错 key 只有运行时才炸；
改成 dataclass 后静态检查就能拦住。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable

#: 正常说完一整段话。
STOP_END_TURN = "end_turn"
#: 模型要求调用工具（上层应当执行工具并把结果喂回去）。
STOP_TOOL_USE = "tool_use"
#: 撞到 max_tokens 上限。
STOP_MAX_TOKENS = "max_tokens"
#: 被 cancel token 主动中断。
STOP_CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class ThinkingDelta:
    """模型思考增量（Anthropic extended thinking / 推理模型的 reasoning 字段）。

    只用于展示进度，**不**进对话历史。
    """

    type: ClassVar[str] = "thinking_delta"

    text: str


@dataclass(slots=True, frozen=True)
class TextDelta:
    """模型正文增量。"""

    type: ClassVar[str] = "text_delta"

    text: str


@dataclass(slots=True, frozen=True)
class ToolUse:
    """模型请求调用一个工具。`input` 已经解析成 dict。"""

    type: ClassVar[str] = "tool_use"

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Done:
    """一次流式调用的正常结束。`stop_reason` 取上面的 STOP_* 常量。"""

    type: ClassVar[str] = "done"

    stop_reason: str = STOP_END_TURN

    @property
    def has_tool_use(self) -> bool:
        return self.stop_reason == STOP_TOOL_USE

    @property
    def cancelled(self) -> bool:
        return self.stop_reason == STOP_CANCELLED


@dataclass(slots=True, frozen=True)
class ErrorEvent:
    """流中断。

    流一旦开始就无法再向调用方抛异常（调用方多半已经 yield 出去了），
    所以错误以事件形式给出；`detail` 只进日志，`message` 可展示。

    注意：**调用开始前**的失败（例如没配 key）会抛 `LlmUnavailableError`，
    不是 ErrorEvent —— 那种情况本就不该开始一次流。
    """

    type: ClassVar[str] = "error"

    message: str
    detail: str = ""


#: LLM 流式事件联合。
LLMEvent = ThinkingDelta | TextDelta | ToolUse | Done | ErrorEvent


@runtime_checkable
class CancelToken(Protocol):
    """取消信号。

    只要实现 `is_set()` 即可 —— `threading.Event` / `asyncio.Event` /
    `anyio.Event` 都满足，调用方不必关心用哪种。
    """

    def is_set(self) -> bool: ...


def is_cancelled(cancel: CancelToken | None) -> bool:
    """空 token 视为"未取消"。"""
    return cancel is not None and cancel.is_set()


__all__ = [
    "STOP_CANCELLED",
    "STOP_END_TURN",
    "STOP_MAX_TOKENS",
    "STOP_TOOL_USE",
    "CancelToken",
    "Done",
    "ErrorEvent",
    "LLMEvent",
    "TextDelta",
    "ThinkingDelta",
    "ToolUse",
    "is_cancelled",
]
