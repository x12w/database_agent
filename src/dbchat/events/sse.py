"""SSE 分帧 —— 全项目唯一对浏览器 `json.dumps` 的地方。

统一在这里出口，是为了保证：
* 线上事件永远是 ChatEvent（不可能漏掉某个未投影的内部对象）；
* 心跳统一发送，长耗时阶段（首次 schema 加载可能数十秒）连接不会被中间设备掐断；
* 事件名就是 `type`（点号分段），不再需要 `error` → `explore_error` 那种改名 hack。
"""

from __future__ import annotations

import json
from typing import Iterable

from dbchat.events.chat import ChatEvent

#: 心跳间隔（秒）。前端据此判断连接存活。
HEARTBEAT_SECONDS = 15


def to_sse(event: ChatEvent) -> str:
    """把一个线上事件编码成一帧 SSE。"""
    data = event.model_dump_json(exclude_none=False)
    return f"event: {event.type}\ndata: {data}\n\n"


def to_sse_raw(event_type: str, payload: dict) -> str:
    """编码一帧非 ChatEvent 的 SSE（仅用于探索流这类独立通道）。"""
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def heartbeat() -> str:
    """SSE 注释帧。客户端会忽略，但能保活连接。"""
    return ": ping\n\n"


SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，否则流式会攒成一坨
}


__all__ = ["HEARTBEAT_SECONDS", "SSE_HEADERS", "heartbeat", "to_sse", "to_sse_raw", "Iterable"]
