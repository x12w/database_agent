"""SSE 响应构造 —— 心跳、断连检测与帧编码的时机。

帧编码本身在 `dbchat.events.sse`（全项目唯一对浏览器 `json.dumps` 的地方），
这里只负责"什么时候发、发什么、什么时候停"。

两件历史实现没做的事：

* **心跳**：首次 schema 加载可能几十秒没有任何事件，旧实现期间一个字节都不发，
  前端只能自己设个超时猜连接是不是死了（`app/main.py` 那个 180s 的
  `asyncio.wait_for` 就是这个思路的补丁）。这里按 `HEARTBEAT_SECONDS` 发注释帧。
* **断连检测**：旧实现里用户关掉页面之后，那一轮仍会把 LLM 调用、工具任务、
  数据库查询全跑完。这里主动发现断开并取消运行，让用例的 `finally`（工具任务、
  连接租约）立刻收尾。

用例的生成器由**一个固定任务**驱动（`produce`）。这一点很关键：async generator
的每一段都在调用它的任务里执行，而 anyio 的 cancel scope 会记住"进入时所在
的任务"，换个任务继续迭代就会炸 `Attempted to exit cancel scope in a different
task` —— 心跳/断连这些"插入动作"因此都不能直接嵌在 `__anext__` 之间。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import cast

import anyio
from fastapi import Request
from fastapi.responses import StreamingResponse

from dbchat.api.failures import chat_failure, explore_failure
from dbchat.events.base import EventBase
from dbchat.events.chat import CHAT_EVENT_TYPES, ChatEvent
from dbchat.events.explore import EXPLORE_EVENT_TYPES
from dbchat.events.sse import HEARTBEAT_SECONDS, SSE_HEADERS, heartbeat, to_sse, to_sse_raw

logger = logging.getLogger(__name__)

MEDIA_TYPE = "text/event-stream"

#: 断连轮询间隔（秒）。`Request.is_disconnected()` 只是看一眼接收通道里有没有
#: 已排队的中断消息（starlette 把它包在一个立刻取消的作用域里），不会阻塞，
#: 所以可以问得比心跳频繁得多。
DISCONNECT_POLL_SECONDS = 1.0

#: 事件队列容量。生产端（用例）比消费端（网络）快时靠它缓冲。
QUEUE_SIZE = 64

#: 已编码的帧 + 它的事件序号（失败帧接着这个序号往下编号）。
Frame = tuple[str, int | None]
EncodeFn = Callable[[object], "Frame | None"]
#: 失败帧构造器：(异常, 序号, run_id) → 一帧待发送的 SSE 文本。
FailureFn = Callable[[BaseException, int, str], str]


@dataclass(slots=True)
class _ProducerError:
    """生产端抛出的异常 —— 由消费端转成线上失败事件。"""

    exc: BaseException


def _chat_frame(item: object) -> Frame | None:
    """线上事件 → SSE 帧；契约外的对象丢弃并记日志。

    绝不把内部对象（AgentEvent、原始 dict）发给浏览器：能出网的只有
    `ChatEvent` 这 13 种。
    """
    if isinstance(item, EventBase) and item.type in CHAT_EVENT_TYPES:
        return to_sse(cast(ChatEvent, item)), item.seq
    logger.warning("丢弃契约外的聊天事件: %s", type(item).__name__)
    return None


def _explore_frame(item: object) -> Frame | None:
    """探索通道事件 → SSE 帧。

    探索通道的契约是 `ExploreEvent`（5 种）。`ExploreUseCase.run` 的返回类型是
    `object`，实现方可能产出 dict，这里一并接住 —— 但事件名仍然只认契约里的
    那几个（历史实现的 `progress`/`admin_question`/`done` 从未被契约化）。
    """
    if isinstance(item, EventBase) and item.type in EXPLORE_EVENT_TYPES:
        return to_sse_raw(item.type, item.model_dump(mode="json")), item.seq
    if isinstance(item, dict) and isinstance(item.get("type"), str):
        seq = item.get("seq")
        return to_sse_raw(str(item["type"]), item), int(seq) if isinstance(seq, int) else None
    logger.warning("丢弃契约外的探索事件: %s", type(item).__name__)
    return None


async def _frames(
    request: Request,
    source: AsyncIterator[object],
    *,
    encode: EncodeFn,
    on_failure: FailureFn,
    run_id: str,
    label: str,
) -> AsyncIterator[str]:
    """把事件流转成一串 SSE 帧，期间发心跳、盯着断连。"""
    send, receive = anyio.create_memory_object_stream[object](QUEUE_SIZE)
    heartbeat_at = time.monotonic() + HEARTBEAT_SECONDS
    last_seq = 0

    async def produce() -> None:
        """驱动用例的生成器（全程同一个任务，见模块 docstring）。"""
        try:
            async for item in source:
                await send.send(item)
        except anyio.ClosedResourceError:
            # 消费端已退出（客户端断开）：静默收尾，别再打扰它。
            logger.debug("%s：消费端已关闭，停止转发 (run_id=%s)", label, run_id)
        except Exception as exc:  # noqa: BLE001 - 转成线上失败事件，不让连接半途而废
            logger.exception("%s：事件流中断 (run_id=%s)", label, run_id)
            try:
                await send.send(_ProducerError(exc))
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                logger.debug("%s：失败事件没能送达（消费端已退出）", label)
        finally:
            await send.aclose()

    async with anyio.create_task_group() as tg:
        tg.start_soon(produce)
        while True:
            now = time.monotonic()
            if now >= heartbeat_at:
                heartbeat_at = now + HEARTBEAT_SECONDS
                yield heartbeat()
                if await request.is_disconnected():
                    logger.info("%s：客户端断开，取消运行 (run_id=%s)", label, run_id)
                    tg.cancel_scope.cancel()
                    break
                continue

            slice_s = min(DISCONNECT_POLL_SECONDS, heartbeat_at - now)
            item: object = None
            try:
                with anyio.move_on_after(slice_s) as scope:
                    item = await receive.receive()
            except anyio.EndOfStream:
                break  # 生产端正常结束
            if scope.cancel_called:
                # 这段时间没有事件：看看客户端还在不在。
                if await request.is_disconnected():
                    logger.info("%s：客户端断开，取消运行 (run_id=%s)", label, run_id)
                    tg.cancel_scope.cancel()
                    break
                continue

            if isinstance(item, _ProducerError):
                yield on_failure(item.exc, last_seq + 1, run_id)
                break
            if isinstance(item, EventBase) and getattr(item, "run_id", None):
                # 失败帧跟着最后一条事件的 run_id，前端才能把它挂到同一次运行上。
                run_id = item.run_id
            frame = encode(item)
            if frame is None:
                continue
            text, seq = frame
            if seq is not None:
                last_seq = max(last_seq, seq)
            yield text


def _response(frames: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(frames, media_type=MEDIA_TYPE, headers=dict(SSE_HEADERS))


def chat_stream_response(
    request: Request, source: AsyncIterator[object], *, run_id: str, debug: bool = False
) -> StreamingResponse:
    """聊天通道的 SSE 响应。"""

    def failure(exc: BaseException, seq: int, failed_run: str) -> str:
        return to_sse(chat_failure(exc, run_id=failed_run, seq=seq, debug=debug))

    return _response(
        _frames(
            request,
            source,
            encode=_chat_frame,
            on_failure=failure,
            run_id=run_id,
            label="聊天",
        )
    )


def explore_stream_response(
    request: Request, source: AsyncIterator[object], *, run_id: str, debug: bool = False
) -> StreamingResponse:
    """探索通道的 SSE 响应。"""

    def failure(exc: BaseException, seq: int, failed_run: str) -> str:
        event = explore_failure(exc, run_id=failed_run, seq=seq, debug=debug)
        return to_sse_raw(event.type, event.model_dump(mode="json"))

    return _response(
        _frames(
            request,
            source,
            encode=_explore_frame,
            on_failure=failure,
            run_id=run_id,
            label="探索",
        )
    )


__all__ = ["chat_stream_response", "explore_stream_response"]
