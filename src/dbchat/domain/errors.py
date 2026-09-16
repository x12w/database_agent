"""统一异常类型。

设计原则：错误必须携带**可直接展示给用户的文案**（zh-CN）和**只进日志的原始细节**。
历史代码里大量 `except Exception: pass` 把两类信息一起丢了，这里用类型强制区分。
"""

from __future__ import annotations

from enum import StrEnum


class DbErrorKind(StrEnum):
    """数据库错误分类 —— 决定给用户看什么文案、是否值得重试。"""

    AUTH = "auth"
    NETWORK = "network"
    NOT_FOUND = "not_found"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    SYNTAX = "syntax"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


#: 每类错误的用户可见文案。原始驱动报文只进日志，不进这里。
DB_ERROR_MESSAGES: dict[DbErrorKind, str] = {
    DbErrorKind.AUTH: "数据库用户名或密码错误，请检查连接配置",
    DbErrorKind.NETWORK: "无法连接到数据库，请确认数据库服务已启动且网络可达",
    DbErrorKind.NOT_FOUND: "数据库或表不存在",
    DbErrorKind.PERMISSION: "当前账号没有访问该对象的权限",
    DbErrorKind.TIMEOUT: "数据库查询超时，请缩小查询范围后重试",
    DbErrorKind.SYNTAX: "生成的查询语句无法被数据库解析",
    DbErrorKind.UNSUPPORTED: "当前数据库不支持该操作",
    DbErrorKind.UNKNOWN: "数据库操作失败",
}

#: 值得自动重试的错误类别（换一种查询方式可能成功）。
RETRYABLE_DB_ERRORS = frozenset({DbErrorKind.SYNTAX, DbErrorKind.TIMEOUT})


class DbChatError(Exception):
    """所有本应用异常的基类。

    Attributes:
        user_message: 可直接展示给用户的中文文案。
        detail: 原始信息（驱动报文、堆栈摘要），只写日志。
    """

    #: 线上错误事件使用的错误码。
    code: str = "internal"

    def __init__(self, user_message: str, *, detail: str = "", cause: BaseException | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail
        self.__cause__ = cause

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.user_message


class DbError(DbChatError):
    """数据库相关错误。"""

    code = "db"

    def __init__(
        self,
        kind: DbErrorKind,
        *,
        detail: str = "",
        user_message: str | None = None,
        cause: BaseException | None = None,
    ):
        self.kind = kind
        super().__init__(
            user_message or DB_ERROR_MESSAGES[kind],
            detail=detail,
            cause=cause,
        )

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE_DB_ERRORS


class LlmError(DbChatError):
    """LLM 调用失败。"""

    code = "llm"


class LlmUnavailableError(LlmError):
    """未配置 API key / 客户端不可用。"""

    code = "llm_unavailable"

    def __init__(self, detail: str = ""):
        super().__init__("未配置可用的模型服务，请在 .env 中设置 LLM_API_KEY", detail=detail)


class ToolError(DbChatError):
    """工具执行失败。"""

    code = "tool"


class ValidationError(DbChatError):
    """SQL 校验未通过。"""

    code = "validation"


class ConfigError(DbChatError):
    """配置错误。"""

    code = "config"
