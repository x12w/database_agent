"""消息与工具定义的线格式转换 —— **唯一**一处 provider 差异所在地。

历史上有三份实现做同一件事：

* `app/agent/orchestrator.py` 的 `_add_tool_messages_anthropic/_openai`
* `app/llm/claude_client.py` 的 `_format_anthropic_messages` / `_build_openai_messages`
* 以及各自散落的 tool_calls 拼装

它们对同一份历史的解读并不一致（OpenAI 分支曾经把 assistant 的
`tool_calls` 直接丢掉，工具调用链在第二轮就断了）。这里收敛成一套：

内部规范形态（`Message`）尽量贴近"事实"——助手要调用哪些工具、每个工具
返回了什么——而不是某家 API 的线格式：

* assistant 带工具调用 → `Message(role="assistant", text=..., tool_calls=(ToolCall, ...))`
* 工具结果           → `Message(role="tool", tool_call_id=..., text=...)`

两个 formatter 从同一份 `Message` 列表出发，各自渲染成线格式。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: 单条工具结果进入历史的字符上限。超过就掐头去尾 —— 大库的
#: schema_summary / table_detail 结果可以轻松撑爆上下文窗口。
MAX_TOOL_RESULT_CHARS = 4000
_TRUNCATE_HEAD = 3000
_TRUNCATE_TAIL = 500

#: 未提供 schema 时的兜底参数定义。
_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

#: 内部规范角色。`system` 不进 messages —— 它由 provider 单独接收。
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


@dataclass(slots=True, frozen=True)
class ToolCall:
    """助手发起的一次工具调用（`input` 是已解析的 dict，不是 JSON 串）。"""

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Message:
    """内部规范消息。"""

    role: str
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""


# ---- 构造助手 ----


def user_message(text: str) -> Message:
    """一条用户消息。"""
    return Message(role=ROLE_USER, text=text)


def assistant_message(text: str = "", tool_calls: Sequence[ToolCall] = ()) -> Message:
    """一条助手消息（可同时带正文与工具调用）。"""
    return Message(role=ROLE_ASSISTANT, text=text, tool_calls=tuple(tool_calls))


def tool_result_message(tool_call_id: str, content: str) -> Message:
    """一条工具结果消息 —— 必须与前面对应的 `ToolCall.id` 配对。"""
    return Message(role=ROLE_TOOL, tool_call_id=tool_call_id, text=content)


def truncate_tool_result(content: str) -> str:
    """截断过长的工具结果。

    保留开头（概览、关键指标）与结尾（外键/关联信息），中间省略。
    两个 provider 必须看到**同样**的截断结果，否则同一份历史在切换
    provider 后行为不一致。
    """
    if len(content) <= MAX_TOOL_RESULT_CHARS:
        return content
    dropped = len(content) - _TRUNCATE_HEAD - _TRUNCATE_TAIL
    return (
        content[:_TRUNCATE_HEAD]
        + f"\n\n... [已截断 {dropped} 字符，完整结果可重新调用工具获取] ...\n\n"
        + content[-_TRUNCATE_TAIL:]
    )


# ---- 规范形态还原 ----


def _flatten_content(content: Any) -> str:
    """把线格式的 content（字符串 / 块列表）压成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        return _flatten_block(content)
    if isinstance(content, Sequence):
        parts = [_flatten_content(item) for item in content]
        return "\n\n".join(p for p in parts if p)
    return str(content)


def _flatten_block(block: Mapping[str, Any]) -> str:
    """单个内容块 → 文本。tool_result 块取其 content。"""
    btype = block.get("type")
    if btype == "text":
        return str(block.get("text", ""))
    if btype == "tool_result":
        return _flatten_content(block.get("content", ""))
    if btype == "thinking":
        return str(block.get("thinking", ""))
    if btype == "tool_use":
        return ""
    return _flatten_content(block.get("text", ""))


def _tool_calls_from_openai(raw_calls: Sequence[Any]) -> tuple[ToolCall, ...]:
    """OpenAI 线格式的 tool_calls → ToolCall。"""
    calls: list[ToolCall] = []
    for raw in raw_calls:
        if isinstance(raw, ToolCall):
            calls.append(raw)
            continue
        if not isinstance(raw, Mapping):
            continue
        func = raw.get("function") or {}
        arguments = func.get("arguments", "")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                # 模型吐出非法 JSON 是常见事故，保留原文而不是丢弃整次调用。
                parsed = {"raw": arguments}
        elif isinstance(arguments, Mapping):
            parsed = dict(arguments)
        else:
            parsed = {}
        calls.append(
            ToolCall(id=str(raw.get("id", "")), name=str(func.get("name", "")), input=parsed)
        )
    return tuple(calls)


def _tool_calls_from_anthropic_blocks(blocks: Sequence[Any]) -> tuple[ToolCall, ...]:
    """Anthropic 内容的 tool_use 块 → ToolCall。"""
    calls: list[ToolCall] = []
    for block in blocks:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        raw_input = block.get("input", {})
        calls.append(
            ToolCall(
                id=str(block.get("id", "")),
                name=str(block.get("name", "")),
                input=dict(raw_input) if isinstance(raw_input, Mapping) else {"raw": raw_input},
            )
        )
    return tuple(calls)


def _expand(raw: Any) -> list[Message]:
    """把一条线格式消息展开成 0..n 条规范消息。

    Anthropic 会把工具结果塞在 user 消息的 tool_result 块里；规范形态把
    它们拆成独立的 `tool` 消息，这样两个 formatter 面对的是同一份数据。
    """
    if isinstance(raw, Message):
        return [raw]
    if not isinstance(raw, Mapping):
        raise TypeError(f"消息必须是 Message 或 dict，收到 {type(raw).__name__}")

    role = str(raw.get("role") or ROLE_USER).lower()
    content = raw.get("content", "")

    if role == ROLE_TOOL:
        return [
            tool_result_message(
                str(raw.get("tool_call_id") or raw.get("tool_use_id") or ""),
                _flatten_content(content),
            )
        ]

    if role == ROLE_ASSISTANT:
        calls: tuple[ToolCall, ...] = ()
        if isinstance(content, Sequence) and not isinstance(content, str):
            calls = _tool_calls_from_anthropic_blocks(content)
        if not calls and raw.get("tool_calls"):
            calls = _tool_calls_from_openai(raw["tool_calls"])
        return [assistant_message(_flatten_content(content), calls)]

    # user（或其它角色）：可能夹带 tool_result 块
    out: list[Message] = []
    trailing_text = ""
    if isinstance(content, Sequence) and not isinstance(content, str):
        results = [
            block
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "tool_result"
        ]
        trailing_text = _flatten_content(
            [
                block
                for block in content
                if not (isinstance(block, Mapping) and block.get("type") == "tool_result")
            ]
        )
        for block in results:
            out.append(
                tool_result_message(
                    str(block.get("tool_use_id") or block.get("tool_call_id") or ""),
                    _flatten_content(block.get("content", "")),
                )
            )
    else:
        trailing_text = _flatten_content(content)

    if trailing_text or not out:
        out.append(Message(role=role, text=trailing_text))
    return out


def normalize_messages(raw_messages: Sequence[Any]) -> list[Message]:
    """把任意线格式的历史还原成规范形态。"""
    out: list[Message] = []
    for raw in raw_messages:
        out.extend(_expand(raw))
    return out


# ---- 渲染成 Anthropic 线格式 ----


def to_anthropic_messages(raw_messages: Sequence[Any]) -> list[dict[str, Any]]:
    """渲染成 Anthropic messages。

    Anthropic 的两条硬约束在这里统一处理：

    1. 工具结果必须是 user 消息里的 `tool_result` 块；
    2. 角色必须交替（连续的同类消息要合并）。
    """
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush_results() -> None:
        if not pending_results:
            return
        blocks = list(pending_results)
        pending_results.clear()
        # tool_result 必须排在 user 消息内容的最前面；上一条恰好也是
        # 块形式的 user 消息时直接并进去，避免出现连续两个 user。
        if out and out[-1]["role"] == ROLE_USER and isinstance(out[-1]["content"], list):
            out[-1]["content"] = blocks + out[-1]["content"]
        else:
            out.append({"role": ROLE_USER, "content": blocks})

    def append_text(role: str, text: str) -> None:
        if role == ROLE_USER:
            flush_results()
            # 上一条是刚 flush 出来的 tool_result 消息时，正文要作为
            # 文本块并进同一条 user 消息 —— 否则会出现连续两个 user，
            # Anthropic 直接拒绝。
            if out and out[-1]["role"] == ROLE_USER and isinstance(out[-1]["content"], list):
                out[-1]["content"].append({"type": "text", "text": text})
                return
        if out and out[-1]["role"] == role and isinstance(out[-1]["content"], str):
            out[-1]["content"] = f"{out[-1]['content']}\n\n{text}"
            return
        out.append({"role": role, "content": text})

    for msg in normalize_messages(raw_messages):
        if msg.role == ROLE_TOOL:
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": truncate_tool_result(msg.text),
                }
            )
            continue

        if msg.role == ROLE_ASSISTANT:
            flush_results()
            if msg.tool_calls:
                blocks: list[dict[str, Any]] = []
                if msg.text:
                    blocks.append({"type": "text", "text": msg.text})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.input,
                    }
                    for call in msg.tool_calls
                )
                out.append({"role": ROLE_ASSISTANT, "content": blocks})
            else:
                append_text(ROLE_ASSISTANT, msg.text)
            continue

        append_text(msg.role, msg.text)

    flush_results()
    return out


# ---- 渲染成 OpenAI 线格式 ----


def to_openai_messages(system: str, raw_messages: Sequence[Any]) -> list[dict[str, Any]]:
    """渲染成 OpenAI-compatible messages。

    助手消息的 `tool_calls` **必须**保留 —— 丢掉它，下一轮请求里
    `role="tool"` 的消息就会因为没有对应的 tool_call_id 而报 400，
    历史实现正是在这里断链的。
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in normalize_messages(raw_messages):
        if msg.role == ROLE_TOOL:
            out.append(
                {
                    "role": ROLE_TOOL,
                    "tool_call_id": msg.tool_call_id,
                    "content": truncate_tool_result(msg.text),
                }
            )
            continue

        if msg.role == ROLE_ASSISTANT and msg.tool_calls:
            out.append(
                {
                    "role": ROLE_ASSISTANT,
                    "content": msg.text or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.input, ensure_ascii=False),
                            },
                        }
                        for call in msg.tool_calls
                    ],
                }
            )
            continue

        out.append({"role": msg.role, "content": msg.text})

    return out


# ---- 工具定义 ----


def normalize_tool_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """把工具定义归一成 Anthropic 形状（`name` / `description` / `input_schema`）。

    已经是 OpenAI 形状的定义会被拆开 —— 历史实现两种形状混着传。
    """
    if spec.get("type") == "function" and isinstance(spec.get("function"), Mapping):
        spec = spec["function"]
    schema = spec.get("input_schema") or spec.get("parameters") or _EMPTY_SCHEMA
    return {
        "name": str(spec.get("name", "")),
        "description": str(spec.get("description", "")),
        "input_schema": dict(schema) if isinstance(schema, Mapping) else _EMPTY_SCHEMA,
    }


def to_anthropic_tools(specs: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """渲染成 Anthropic tools。

    入参是 **Anthropic 形状的普通 dict**（`{"name", "description", "input_schema"}`），
    这样 `dbchat.tools` 不必反向依赖本模块就能构造工具定义。
    """
    if not specs:
        return []
    return [normalize_tool_spec(spec) for spec in specs]


def to_openai_tools(specs: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """渲染成 OpenAI-compatible tools。"""
    if not specs:
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["input_schema"],
            },
        }
        for spec in (normalize_tool_spec(spec) for spec in specs)
    ]


__all__ = [
    "MAX_TOOL_RESULT_CHARS",
    "ROLE_ASSISTANT",
    "ROLE_TOOL",
    "ROLE_USER",
    "Message",
    "ToolCall",
    "assistant_message",
    "normalize_messages",
    "normalize_tool_spec",
    "to_anthropic_messages",
    "to_anthropic_tools",
    "to_openai_messages",
    "to_openai_tools",
    "tool_result_message",
    "truncate_tool_result",
    "user_message",
]
