"""异常 → 线上失败事件。

两条硬规矩：

* 给用户看的只有中文文案 —— 来自 `DbChatError.user_message` 或错误码对应的
  固定说明，**绝不**把驱动报文/堆栈放进 `message`；
* 原始细节（类型名 + 报文）进日志，调试模式下再附到 `error_detail`，
  前端默认收起。

历史实现把 `str(e)` 直接塞进 SSE 的 `error` 事件里，用户看到的是
`(1045, "Access denied for user 'root'@'localhost'")` 这种既看不懂、又
泄漏主机信息的文本。
"""

from __future__ import annotations

import logging
import time

from dbchat.domain.errors import (
    DbChatError,
    DbError,
    DbErrorKind,
    LlmError,
    LlmUnavailableError,
    ValidationError,
)
from dbchat.events.base import RETRYABLE_ERRORS, ErrorCode
from dbchat.events.chat import RunFailed
from dbchat.events.explore import ExploreFailed
from dbchat.events.sse import to_sse

logger = logging.getLogger(__name__)

__all__ = ["ApiFailure", "chat_failure", "classify_failure", "explore_failure", "single_frame"]


class ApiFailure(Exception):
    """入参不合法 —— 由异常处理器转成一帧 `run.failed`。

    为什么用异常而不是直接返回 4xx：前端是用 `fetch` 消费这条 SSE 的，
    一帧失败事件能直接渲染成"这次提问失败了，原因是……"，而状态码只能变成
    "SSE 连接失败：422"。为什么用异常而不是在路由体里判断：依赖先于路由体
    执行，靠它才能让"提问为空"的判定排在"用例是否就绪"之前。
    """

    def __init__(self, code: ErrorCode, message: str, *, run_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_id = run_id


def single_frame(failure: ApiFailure) -> str:
    """把入参错误编成一帧 SSE（`run.failed`）。"""
    return to_sse(
        RunFailed(
            seq=1,
            ts=time.time(),
            run_id=failure.run_id or "invalid",
            code=failure.code,
            message=failure.message,
            retryable=False,
        )
    )

#: 兜底文案 —— 用户不需要知道是哪一个模块炸的。
INTERNAL_MESSAGE = "服务内部错误，请稍后重试"
TIMEOUT_MESSAGE = "处理超时，请缩小问题的范围后重试"
CANCELLED_MESSAGE = "本次提问已取消"

#: `DbErrorKind` → 线上错误码。数据库的细分原因不外泄，只按用户能采取的行动分类。
_DB_ERROR_CODES: dict[DbErrorKind, ErrorCode] = {
    DbErrorKind.AUTH: ErrorCode.DB_AUTH,
    DbErrorKind.NETWORK: ErrorCode.DB_UNREACHABLE,
    DbErrorKind.TIMEOUT: ErrorCode.DB_TIMEOUT,
    DbErrorKind.NOT_FOUND: ErrorCode.DB_QUERY_FAILED,
    DbErrorKind.PERMISSION: ErrorCode.DB_QUERY_FAILED,
    DbErrorKind.SYNTAX: ErrorCode.DB_QUERY_FAILED,
    DbErrorKind.UNSUPPORTED: ErrorCode.DB_QUERY_FAILED,
    DbErrorKind.UNKNOWN: ErrorCode.DB_QUERY_FAILED,
}


def classify_failure(exc: BaseException) -> tuple[ErrorCode, str, str]:
    """把异常归类成 `(错误码, 用户文案, 原始细节)`。

    原始细节只用于日志与调试模式，调用方负责决定要不要随事件下发。
    """
    detail = f"{type(exc).__name__}: {exc}"

    if isinstance(exc, LlmUnavailableError):
        return ErrorCode.LLM_UNAVAILABLE, exc.user_message, detail
    if isinstance(exc, LlmError):
        return ErrorCode.LLM_FAILED, exc.user_message, detail
    if isinstance(exc, DbError):
        code = _DB_ERROR_CODES.get(exc.kind, ErrorCode.DB_QUERY_FAILED)
        return code, exc.user_message, exc.detail or detail
    if isinstance(exc, ValidationError):
        return ErrorCode.VALIDATION, exc.user_message, exc.detail or detail
    if isinstance(exc, DbChatError):
        return ErrorCode.INTERNAL, exc.user_message, exc.detail or detail
    if isinstance(exc, TimeoutError):
        return ErrorCode.TIMEOUT, TIMEOUT_MESSAGE, detail
    return ErrorCode.INTERNAL, INTERNAL_MESSAGE, detail


def chat_failure(
    exc: BaseException, *, run_id: str, seq: int, debug: bool = False
) -> RunFailed:
    """构造一条 `run.failed`（聊天通道的收尾事件）。"""
    code, message, detail = classify_failure(exc)
    logger.warning(
        "运行失败 (run_id=%s, code=%s): %s | %s", run_id, code.value, message, detail
    )
    return RunFailed(
        seq=seq,
        ts=time.time(),
        run_id=run_id,
        code=code,
        message=message,
        retryable=code in RETRYABLE_ERRORS,
        error_detail=detail if debug else None,
    )


def explore_failure(
    exc: BaseException, *, run_id: str, seq: int, debug: bool = False
) -> ExploreFailed:
    """构造一条 `explore.failed`（探索通道的收尾事件）。"""
    code, message, detail = classify_failure(exc)
    logger.warning(
        "探索失败 (run_id=%s, code=%s): %s | %s", run_id, code.value, message, detail
    )
    return ExploreFailed(
        seq=seq,
        ts=time.time(),
        run_id=run_id,
        code=code,
        message=message,
        error_detail=detail if debug else None,
    )
