"""Anthropic 线协议实现。

只负责"SDK 事件 → 统一 LLMEvent"的翻译，不认识业务。
消息与工具定义一律经由 `llm/messages.py` 转换。

历史实现用的是**同步** SDK（`anthropic.Anthropic`）并在 async 生成器里
直接 `for event in stream` 迭代 —— 整条流式响应期间事件循环被占死，
并发请求全部排队。这里换成 `AsyncAnthropic`。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

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
    is_cancelled,
)
from dbchat.llm.messages import to_anthropic_messages, to_anthropic_tools
from dbchat.llm.providers._compat import supported_kwargs

logger = logging.getLogger(__name__)


def _map_stop_reason(raw: str | None, *, saw_tool_use: bool) -> str:
    """Anthropic 的 stop_reason → 统一取值。"""
    if saw_tool_use:
        return STOP_TOOL_USE
    if raw == "max_tokens":
        return STOP_MAX_TOKENS
    if raw == "tool_use":
        return STOP_TOOL_USE
    return STOP_END_TURN


async def stream(
    client: Any,
    *,
    model: str,
    system: str,
    messages: Sequence[Any],
    tools: Sequence[Any] = (),
    max_tokens: int,
    temperature: float,
    cancel: CancelToken | None = None,
) -> AsyncIterator[LLMEvent]:
    """流式调用。事件按 provider 无关的统一词汇表产出。"""
    api_messages = to_anthropic_messages(messages)
    api_tools = to_anthropic_tools(tools)

    # 工具调用的入参是分片下发的（input_json_delta），按内容块下标累积。
    partial_input: dict[int, str] = {}
    block_tools: dict[int, tuple[str, str]] = {}
    saw_tool_use = False

    try:
        # 新版 SDK 可能不再接受 temperature 之类的参数，按签名过滤一次。
        stream_kwargs = supported_kwargs(
            client.messages.stream,
            model=model,
            system=system,
            messages=api_messages,
            tools=api_tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        async with client.messages.stream(**stream_kwargs) as message_stream:
            async for event in message_stream:
                if is_cancelled(cancel):
                    yield Done(STOP_CANCELLED)
                    return

                etype = getattr(event, "type", "")
                index = getattr(event, "index", 0) or 0

                if etype == "content_block_start":
                    block = event.content_block
                    if getattr(block, "type", "") == "tool_use":
                        block_tools[index] = (block.id, block.name)
                        partial_input[index] = ""

                elif etype == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", "")
                    if dtype == "thinking_delta":
                        yield ThinkingDelta(getattr(delta, "thinking", "") or "")
                    elif dtype == "text_delta":
                        yield TextDelta(getattr(delta, "text", "") or "")
                    elif dtype == "input_json_delta":
                        partial_input[index] = partial_input.get(index, "") + getattr(
                            delta, "partial_json", ""
                        )

                elif etype == "content_block_stop" and index in block_tools:
                    tool_id, tool_name = block_tools.pop(index)
                    raw_input = partial_input.pop(index, "")
                    try:
                        parsed = json.loads(raw_input) if raw_input.strip() else {}
                    except json.JSONDecodeError:
                        # 模型偶尔吐出截断/非法的 JSON。保留原文交给上层判断，
                        # 直接丢弃会让工具调用凭空消失。
                        logger.warning(
                            "Anthropic 工具 %s 的入参不是合法 JSON（%d 字符），按原文透传",
                            tool_name,
                            len(raw_input),
                        )
                        parsed = {"raw": raw_input}
                    saw_tool_use = True
                    yield ToolUse(id=tool_id, name=tool_name, input=parsed)

                elif etype == "error":
                    detail = str(getattr(event, "error", ""))
                    logger.warning("Anthropic 流内错误: %s", detail)
                    yield ErrorEvent("模型服务返回流内错误", detail=detail)
                    return

            final = await message_stream.get_final_message()

        yield Done(_map_stop_reason(getattr(final, "stop_reason", None), saw_tool_use=saw_tool_use))

    except Exception as exc:
        logger.warning("Anthropic 流式调用失败: %s", exc, exc_info=True)
        yield ErrorEvent("模型调用失败", detail=f"{type(exc).__name__}: {exc}")


async def complete(
    client: Any,
    *,
    model: str,
    system: str,
    messages: Sequence[Any],
    max_tokens: int,
    temperature: float,
) -> str:
    """非流式调用，返回正文文本。

    存在的意义：短判定（结果评估、计算规划、意图分类）不需要流，
    但**必须**是异步的 —— 历史实现直接调同步 `.create()`，
    在事件循环线程上阻塞到响应返回。
    """
    api_messages = to_anthropic_messages(messages)
    create_kwargs = supported_kwargs(
        client.messages.create,
        model=model,
        system=system,
        messages=api_messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    response = await client.messages.create(**create_kwargs)
    parts = [
        getattr(block, "text", "")
        for block in getattr(response, "content", [])
        if getattr(block, "type", "") == "text"
    ]
    return "".join(parts).strip()


__all__ = ["complete", "stream"]
