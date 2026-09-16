"""OpenAI-compatible 线协议实现（OpenAI / DeepSeek / 各类兼容网关）。

只负责"SDK chunk → 统一 LLMEvent"的翻译。
同样换用了 `AsyncOpenAI`：历史实现用同步 client + 同步迭代，
整个流式响应期间阻塞事件循环。
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
from dbchat.llm.messages import to_openai_messages, to_openai_tools
from dbchat.llm.providers._compat import supported_kwargs

logger = logging.getLogger(__name__)


def _map_finish_reason(raw: str | None, *, saw_tool_use: bool) -> str:
    """OpenAI 的 finish_reason → 统一取值。"""
    if raw == "tool_calls" or saw_tool_use:
        return STOP_TOOL_USE
    if raw == "length":
        return STOP_MAX_TOKENS
    return STOP_END_TURN


def _accumulate_tool_calls(acc: dict[int, dict[str, str]], delta: Any) -> None:
    """把增量式的 tool_call 分片按下标累积。

    函数名只在第一个分片里出现，arguments 是逐字下发的 JSON 片段。
    """
    for chunk in delta.tool_calls or []:
        index = getattr(chunk, "index", 0) or 0
        slot = acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if getattr(chunk, "id", None):
            slot["id"] = chunk.id
        function = getattr(chunk, "function", None)
        if function is None:
            continue
        if getattr(function, "name", None):
            slot["name"] = function.name
        if getattr(function, "arguments", None):
            slot["arguments"] += function.arguments


def _parse_arguments(raw: str, tool_name: str) -> dict[str, Any]:
    """工具入参 JSON → dict，失败时保留原文。"""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "OpenAI 工具 %s 的入参不是合法 JSON（%d 字符），按原文透传", tool_name, len(raw)
        )
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"raw": parsed}


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
    """流式调用。"""
    api_messages = to_openai_messages(system, messages)
    api_tools = to_openai_tools(tools)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": api_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if api_tools:
        kwargs["tools"] = api_tools
        kwargs["tool_choice"] = "auto"

    tool_calls_acc: dict[int, dict[str, str]] = {}
    saw_tool_use = False

    try:
        # 不同版本的 OpenAI SDK 对 `max_tokens` 的取舍不一致（部分新版本
        # 只认 `max_completion_tokens`），按签名过滤避免整条链路 TypeError。
        chunk_stream = await client.chat.completions.create(
            **supported_kwargs(client.chat.completions.create, **kwargs)
        )
        try:
            async for chunk in chunk_stream:
                if is_cancelled(cancel):
                    yield Done(STOP_CANCELLED)
                    return

                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                if delta is not None:
                    # 推理模型（DeepSeek-R1 等）把思维链放在 reasoning_content，
                    # 与 Anthropic 的 thinking_delta 归为同一类事件。
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield ThinkingDelta(reasoning)
                    if getattr(delta, "content", None):
                        yield TextDelta(delta.content)
                    if getattr(delta, "tool_calls", None):
                        _accumulate_tool_calls(tool_calls_acc, delta)

                finish = getattr(choice, "finish_reason", None)
                if finish is not None:
                    for index in sorted(tool_calls_acc):
                        slot = tool_calls_acc.pop(index)
                        saw_tool_use = True
                        yield ToolUse(
                            id=slot["id"],
                            name=slot["name"],
                            input=_parse_arguments(slot["arguments"], slot["name"]),
                        )
                    yield Done(_map_finish_reason(finish, saw_tool_use=saw_tool_use))
                    return
        finally:
            # 提前 return（取消/异常）时也要把 HTTP 连接还回去。
            await chunk_stream.close()

        # 流自然结束但没给 finish_reason：把已累积的工具调用交出去，
        # 否则这次工具调用会凭空消失（历史实现在这里直接收尾）。
        for index in sorted(tool_calls_acc):
            slot = tool_calls_acc.pop(index)
            saw_tool_use = True
            yield ToolUse(
                id=slot["id"], name=slot["name"], input=_parse_arguments(slot["arguments"], slot["name"])
            )
        if saw_tool_use:
            yield Done(STOP_TOOL_USE)
        else:
            yield Done(STOP_END_TURN)

    except Exception as exc:
        logger.warning("OpenAI 流式调用失败: %s", exc, exc_info=True)
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
    """非流式调用，返回正文文本。"""
    response = await client.chat.completions.create(
        **supported_kwargs(
            client.chat.completions.create,
            model=model,
            messages=to_openai_messages(system, messages),
            max_tokens=max_tokens,
            temperature=temperature,
        )
    )
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    return (getattr(choices[0].message, "content", None) or "").strip()


__all__ = ["complete", "stream"]
