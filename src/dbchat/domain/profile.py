"""数据库连接配置。

这一层是纯数据：不知道驱动、不知道方言实现，只描述"连到哪里"。
真正的方言行为在 `dbchat.db.dialects` 中。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DialectKind(StrEnum):
    """支持的数据库种类。

    新增国产库时在这里加一个成员，并在 dialects 注册表中注册对应实现。
    """

    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    DAMENG = "dameng"
    # ---- 预留扩展点（尚未实现，注册表会给出明确报错）----
    KINGBASE = "kingbase"
    OPENGAUSS = "opengauss"
    OCEANBASE = "oceanbase"


#: 同一 DialectKind 的常见别名，用于容错解析用户/历史配置里写的 db_type。
_DIALECT_ALIASES: dict[str, DialectKind] = {
    "mysql": DialectKind.MYSQL,
    "mariadb": DialectKind.MYSQL,
    "postgresql": DialectKind.POSTGRESQL,
    "postgres": DialectKind.POSTGRESQL,
    "pg": DialectKind.POSTGRESQL,
    "dameng": DialectKind.DAMENG,
    "dm": DialectKind.DAMENG,
    "dm8": DialectKind.DAMENG,
    "kingbase": DialectKind.KINGBASE,
    "kingbasees": DialectKind.KINGBASE,
    "opengauss": DialectKind.OPENGAUSS,
    "gaussdb": DialectKind.OPENGAUSS,
    "oceanbase": DialectKind.OCEANBASE,
    "ob": DialectKind.OCEANBASE,
}


def parse_dialect_kind(raw: str | None) -> DialectKind:
    """把配置里的 db_type 字符串解析成 DialectKind。

    未知取值显式报错而不是悄悄退回 MySQL —— 静默降级会让一个国产库
    被当成 MySQL 去查询，产生的是错误答案而不是错误提示。
    """
    if not raw:
        return DialectKind.MYSQL
    key = raw.strip().lower()
    try:
        return _DIALECT_ALIASES[key]
    except KeyError:
        supported = ", ".join(sorted({k.value for k in DialectKind}))
        raise ValueError(f"不支持的数据库类型 {raw!r}；已支持: {supported}") from None


class DatabaseProfile(BaseModel):
    """一个数据库连接配置。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    name: str = ""
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""
    kind: DialectKind = DialectKind.MYSQL
    description: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.database or self.id

    def redacted(self) -> DatabaseProfile:
        """去掉密码的副本，用于日志与 API 响应。"""
        return self.model_copy(update={"password": ""})
