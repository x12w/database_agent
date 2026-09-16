"""数据库方言抽象。

这是"国产数据库适配长期有效"的落点。历史上方言逻辑以 `is_pg`/`is_dm`
分支散落在 schema_cache.py（七处）、tools.py、skills.py、main.py、
orchestrator.py 中，同一份列表查询 SQL 有四份副本。

约定：**Dialect 之外的任何代码不得出现 `if db_type == ...`**。
新增一个国产库 = 新增一个子类 + 在注册表里加一行，不改其他文件。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from dbchat.domain.errors import DbErrorKind
from dbchat.domain.profile import DatabaseProfile, DialectKind
from dbchat.domain.query import BoundSQL, TypeFamily
from dbchat.domain.schema import CatalogScope


class RowLimitStyle(StrEnum):
    """行数限制的书写方式。"""

    LIMIT = "limit"                # MySQL / PostgreSQL: LIMIT n
    FETCH_FIRST = "fetch_first"    # Oracle 12c+ / 达梦: FETCH FIRST n ROWS ONLY
    ROWNUM = "rownum"              # Oracle 兼容: WHERE ROWNUM <= n


class SampleStrategy(StrEnum):
    """大表抽样方式。"""

    FULL = "full"                  # 直接查
    TABLESAMPLE = "tablesample"    # PostgreSQL: TABLESAMPLE SYSTEM(n)
    ROWNUM = "rownum"              # Oracle 系: ROWNUM 截断
    LIMIT = "limit"


class RowCountMode(StrEnum):
    """行数统计方式。

    APPROX 取自统计信息（快，可能不准）；EXACT 真的 COUNT(*)（慢，准确）。
    """

    APPROX = "approx"
    EXACT = "exact"


@dataclass(slots=True, frozen=True)
class DialectCapabilities:
    """方言能力声明 —— 让上层据此调整行为，而不是到处写 if。"""

    supports_show: bool = False
    supports_describe: bool = False
    supports_explain: bool = False
    supports_tablesample: bool = False
    supports_schemas: bool = True
    supports_comments: bool = True
    #: 统计信息里的行数是否可信。达梦未收集统计信息时 NUM_ROWS 为 0。
    approx_row_count_reliable: bool = True
    #: 表自带的"修改时间"是否可信（MySQL InnoDB 的 UPDATE_TIME 通常为 NULL）。
    table_mtime_reliable: bool = False


class Cursor(Protocol):
    """各方言统一的行游标接口。"""

    def execute(self, sql: str, params: Sequence[Any] | None = ...) -> Any: ...
    def fetchall(self) -> list[Mapping[str, Any]]: ...
    def fetchone(self) -> Mapping[str, Any] | None: ...
    @property
    def description(self) -> Sequence[Any] | None: ...
    def close(self) -> None: ...


class Connection(Protocol):
    """各方言统一的连接接口。"""

    def cursor(self) -> Any: ...
    def close(self) -> None: ...


class Dialect(ABC):
    """一种数据库方言。

    子类通过覆写 `sql_*` 方法提供各自的内省 SQL，通过 `normalize_row`
    抹平行形状差异。上层代码（内省、校验、工具、提示词）只调用这些方法。
    """

    # ---- 身份 ----
    kind: DialectKind
    #: 展示给用户的名字，如"达梦(Dameng)"。
    display_name: str
    #: sqlglot 的方言名，用于 SQL 解析；无对应实现时用最接近的。
    sqlglot_name: str
    #: 驱动包名，用于给出"如何安装驱动"的提示。
    driver_package: str
    capabilities: DialectCapabilities = DialectCapabilities()

    # ---- 连接 ----
    @abstractmethod
    def import_driver(self) -> Any:
        """导入驱动模块。

        Raises:
            DbError: 驱动不可用时，附带可操作的安装/环境提示。
        """

    @abstractmethod
    def connect(self, profile: DatabaseProfile, *, timeout_s: int = 30) -> Connection:
        """建立连接并切到目标 schema。

        Raises:
            DbError: 按 `classify_error` 分类后的错误。
        """

    @abstractmethod
    def ping(self, conn: Connection) -> bool:
        """连接是否存活。"""

    @abstractmethod
    def create_cursor(self, conn: Connection) -> Cursor:
        """创建返回 dict 行的游标。"""

    @abstractmethod
    def classify_error(self, exc: BaseException) -> DbErrorKind:
        """把驱动异常归类 —— 决定给用户看什么文案、是否重试。"""

    # ---- 标识符与参数 ----
    #: 标识符引用字符，如 '`' 或 '"'。
    identifier_quote: str

    def quote(self, ident: str) -> str:
        """引用标识符，**必须**转义标识符内嵌的引用字符。

        历史实现是 `f'"{name}"'`，遇到含双引号的表名会拼出可注入的 SQL。
        """
        q = self.identifier_quote
        escaped = ident.replace(q, q + q)
        return f"{q}{escaped}{q}"

    def qualified(self, schema: str | None, name: str) -> str:
        """拼限定名。"""
        return f"{self.quote(schema)}.{self.quote(name)}" if schema else self.quote(name)

    @property
    def quote_label_zh(self) -> str:
        return "反引号" if self.identifier_quote == "`" else "双引号"

    @abstractmethod
    def placeholder(self) -> str:
        """参数占位符。取自驱动自身的 paramstyle，不靠字符串猜。"""

    @abstractmethod
    def bind(self, sql: str, count: int) -> str:
        """把 `%s` 占位符改写成该方言的形式（字面量感知，不碰字符串内的 `%s`）。"""

    # ---- 行归一化 ----
    @abstractmethod
    def normalize_row(self, row: Any, description: Sequence[Any] | None) -> Mapping[str, Any]:
        """把驱动返回的行变成统一的大小写不敏感 dict。

        各驱动差异：PG 的小写列名、达梦的元组行、MySQL 的 DictCursor。
        """

    @abstractmethod
    def column_names(self, description: Sequence[Any] | None) -> list[str]:
        """从 cursor.description 提取列名。"""

    @abstractmethod
    def coerce_value(self, value: Any) -> Any:
        """把驱动特定类型（Decimal/bytes/datetime）转成可 JSON 化的值。"""

    # ---- 类型 ----
    @abstractmethod
    def type_family(self, data_type: str, column_type: str = "") -> TypeFamily:
        """列类型归到大类，供 agent 判断能否聚合/画图。"""

    # ---- 行数限制 ----
    row_limit_style: RowLimitStyle = RowLimitStyle.LIMIT

    def limit_clause(self, n: int) -> str:
        """行数限制子句。达梦在 Oracle 兼容模式下不认 `LIMIT`。"""
        if self.row_limit_style is RowLimitStyle.FETCH_FIRST:
            return f"FETCH FIRST {n} ROWS ONLY"
        return f"LIMIT {n}"

    # ---- 系统对象 ----
    #: 系统 schema / 目录名，用于把内省查询与用户表区分开。
    system_schemas: frozenset[str] = frozenset()

    def system_table_predicate(self, column: str = "TABLE_NAME") -> str:
        """排除系统表的 SQL 谓词。

        达梦的 `##%` / `SREF%` 魔法过滤收敛到子类，不再散落三处。
        """
        return "1=1"

    def default_schema(self, profile: DatabaseProfile) -> str | None:
        """连接后应当切入的 schema。"""
        return profile.database or None

    # ---- 内省 SQL（全部返回可绑定 SQL）----
    @abstractmethod
    def sql_list_tables(self, scope: CatalogScope) -> BoundSQL:
        """列出表：名称、注释、行数。"""

    @abstractmethod
    def sql_list_columns(self, scope: CatalogScope) -> BoundSQL:
        """列出列：表、列、类型、可空、注释、默认值。"""

    @abstractmethod
    def sql_list_foreign_keys(self, scope: CatalogScope) -> BoundSQL:
        """列出外键关系。"""

    @abstractmethod
    def sql_list_indexes(self, scope: CatalogScope) -> BoundSQL:
        """列出索引。"""

    @abstractmethod
    def sql_row_count(self, table: str, mode: RowCountMode = RowCountMode.EXACT) -> BoundSQL:
        """统计单表行数。"""

    @abstractmethod
    def sql_sample(self, table: str, columns: Sequence[str], n: int) -> BoundSQL:
        """抽样若干行。"""

    def sql_set_statement_timeout(self, ms: int) -> BoundSQL | None:
        """设置语句超时。不支持的方言返回 None。"""
        return None

    @abstractmethod
    def sql_checksum(self, scope: CatalogScope) -> BoundSQL:
        """结构新鲜度检查点。

        返回一组 (对象名, 指纹) 行。**已知局限必须在 docstring 写明**：
        没有任何一种廉价手段能可靠检测任意 DDL/DML 变更，
        因此上层必须同时提供"强制刷新"入口。
        """

    def checksum_is_reliable(self) -> bool:
        """检查点是否足以判定"结构未变"。不可靠时上层会按 TTL 刷新。"""
        return False

    # ---- 提示词 ----
    @abstractmethod
    def prompt_notes(self) -> tuple[str, ...]:
        """注入 SQL agent 提示词的方言注意事项。

        取代散在 prompt_builder 里的"用反引号"/"用双引号"硬编码。
        """

    def sql_dialect_hint(self) -> str:
        """给 LLM 的方言总述（生成 SQL 时用）。"""
        return (
            f"当前连接的是 {self.display_name} 数据库。"
            f"表名和列名请用{self.quote_label_zh}括起来。"
            f"限制返回行数请使用 {self.limit_clause(100)} 的形式。"
        )

    # ---- 校验策略 ----
    #: 该方言下不允许作为首关键字的命令（如 PG 上不允许 SHOW）。
    disallowed_leading_keywords: frozenset[str] = frozenset()

    #: 允许作为首关键字的只读命令。
    allowed_leading_keywords: frozenset[str] = frozenset(
        {"SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC", "TABLE", "VALUES"}
    )

    def apply_row_limit(self, sql: str, n: int) -> str:
        """给缺少限流的查询补上行数上限。

        默认实现基于已解析的 AST 由调用方处理；这里给出文本兜底。
        """
        lowered = sql.lower()
        if any(k in lowered for k in (" limit ", " fetch ", " rownum ", " top ")) or lowered.rstrip().endswith(
            (" limit", " fetch")
        ):
            return sql
        return f"{sql.rstrip().rstrip(';')} {self.limit_clause(n)}"


__all__ = [
    "Connection",
    "Cursor",
    "Dialect",
    "DialectCapabilities",
    "RowCountMode",
    "RowLimitStyle",
    "SampleStrategy",
]
