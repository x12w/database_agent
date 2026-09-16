"""模型接入层。

    events    统一流式事件（provider 无关的词汇表）
    messages  消息 / 工具定义的线格式转换（provider 差异的唯一落点）
    providers 各家的 SDK 事件翻译
    client    LLMClient —— stream / complete / classify

用法::

    from dbchat.llm import get_llm_client

    client = get_llm_client()
    async for event in client.stream(system, messages, tools, cancel):
        ...
"""

from dbchat.llm.client import (
    CLASS_AMBIGUOUS,
    CLASS_DATA_QUESTION,
    CLASS_DOC_QUESTION,
    LLMClient,
    get_llm_client,
)
from dbchat.llm.events import (
    STOP_CANCELLED,
    STOP_END_TURN,
    STOP_MAX_TOKENS,
    STOP_TOOL_USE,
    CancelToken,
    Done,
    ErrorEvent,
    LLMEvent,
    TextDelta,
    ThinkingDelta,
    ToolUse,
)
from dbchat.llm.messages import (
    MAX_TOOL_RESULT_CHARS,
    Message,
    ToolCall,
    assistant_message,
    normalize_messages,
    to_anthropic_messages,
    to_anthropic_tools,
    to_openai_messages,
    to_openai_tools,
    tool_result_message,
    truncate_tool_result,
    user_message,
)

__all__ = [
    "CLASS_AMBIGUOUS",
    "CLASS_DATA_QUESTION",
    "CLASS_DOC_QUESTION",
    "MAX_TOOL_RESULT_CHARS",
    "STOP_CANCELLED",
    "STOP_END_TURN",
    "STOP_MAX_TOKENS",
    "STOP_TOOL_USE",
    "CancelToken",
    "Done",
    "ErrorEvent",
    "LLMClient",
    "LLMEvent",
    "Message",
    "TextDelta",
    "ThinkingDelta",
    "ToolCall",
    "ToolUse",
    "assistant_message",
    "get_llm_client",
    "llm_client",
    "normalize_messages",
    "to_anthropic_messages",
    "to_anthropic_tools",
    "to_openai_messages",
    "to_openai_tools",
    "tool_result_message",
    "truncate_tool_result",
    "user_message",
]


def __getattr__(name: str):
    """惰性提供 `llm_client` —— 兼容历史上 `from ... import llm_client` 的写法，
    但不在 import 阶段就读配置/建客户端。
    """
    if name == "llm_client":
        return get_llm_client()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
