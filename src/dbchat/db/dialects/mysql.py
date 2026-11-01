"""MySQL / MariaDB 方言。

内省 SQL 全部走 `information_schema`，**当前库由 `DATABASE()` 决定** ——
连接时已经把 `profile.database` 交给驱动，服务端自己就知道"当前库"，
因此不需要把库名从 Python 拼进 SQL。表名、列名这类**标识符**无法参数化，
统一经过 `quote()` 转义后拼接。

占位符一律写成 `%s`（本模块的内省 SQL 契约），由 `bind()` 按驱动的
真实 paramstyle 改写；PyMySQL 自己就是 `pyformat`，所以是恒等变换。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from dbchat.db.cursor import coerce_jsonable, mapping_from_row
from dbchat.db.dialects import register_dialect
from dbchat.db.dialects.base import (
    Dialect,
    DialectCapabilities,
    RowCountMode,
    RowLimitStyle,
)
from dbchat.db.drivers import classify_pymysql, driver_placeholder, load_pymysql, log_ping_failure
from dbchat.db.sqltext import rewrite_placeholders, split_qualified_name
from dbchat.domain.errors import DB_ERROR_MESSAGES, DbError, DbErrorKind
from dbchat.domain.profile import DatabaseProfile, DialectKind
from dbchat.domain.query import BoundSQL, TypeFamily
from dbchat.domain.schema import CatalogScope

#: 列类型 -> 大类。键统一小写；MySQL 的 DATA_TYPE 本身已是小写。
_NUMERIC_TYPES = frozenset(
    {
        "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
        "decimal", "numeric", "dec", "fixed", "float", "double", "real",
        "bit", "year",
    }
)
_DATETIME_TYPES = frozenset({"date", "datetime", "timestamp", "time", "year"})
_STRING_TYPES = frozenset(
    {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set"}
)
_BINARY_TYPES = frozenset({"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"})
_JSON_TYPES = frozenset({"json", "geometry"})


@register_dialect
class MySQLDialect(Dialect):
    """MySQL 8.x / MariaDB 方言。"""

    kind = DialectKind.MYSQL
    display_name = "MySQL"
    sqlglot_name = "mysql"
    driver_package = "pymysql"
    identifier_quote = "`"
    row_limit_style = RowLimitStyle.LIMIT

    capabilities = DialectCapabilities(
        supports_show=True,
        supports_describe=True,
        supports_explain=True,
        supports_tablesample=False,
        supports_schemas=True,
        supports_comments=True,
        # information_schema.TABLE_ROWS 是估算值，但对 InnoDB 基本都有值，
        # 不会像达梦未收集统计信息那样整片为 0。
        approx_row_count_reliable=True,
        # InnoDB 的 UPDATE_TIME 常年为 NULL（后台统计线程不更新），不可信。
        table_mtime_reliable=False,
    )

    system_schemas = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
    #: MySQL 不认 Oracle 系的维护命令。
    disallowed_leading_keywords = frozenset({"VACUUM", "REINDEX", "ANALYZE"})

    # ------------------------------------------------------------ 连接

    def import_driver(self) -> Any:
        return load_pymysql()

    def connect(self, profile: DatabaseProfile, *, timeout_s: int = 30) -> Any:
        pymysql = self.import_driver()
        try:
            return pymysql.connect(
                host=profile.host,
                port=profile.port,
                user=profile.user,
                password=profile.password,
                database=profile.database or None,
                charset="utf8mb4",
                connect_timeout=timeout_s,
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
                init_command="SET SESSION TRANSACTION READ ONLY",
            )
        except Exception as exc:
            kind = self.classify_error(exc)
            raise DbError(
                kind,
                detail=f"{type(exc).__name__}: {exc}",
                user_message=(
                    f"连接 {profile.display_name} ({profile.host}:{profile.port}) 失败："
                    f"{DB_ERROR_MESSAGES[kind]}"
                ),
                cause=exc,
            ) from exc

    def ping(self, conn: Any) -> bool:
        """探活。

        刻意用 `reconnect=False` —— 自动重连会把"连接已死"这一事实藏起来，
        重连由 `ConnectionManager` 显式负责。
        """
        try:
            conn.ping(reconnect=False)
            return True
        except Exception as exc:
            log_ping_failure(conn, exc)
            return False

    def create_cursor(self, conn: Any) -> Any:
        return conn.cursor()

    def classify_error(self, exc: BaseException) -> DbErrorKind:
        return classify_pymysql(exc)

    # -------------------------------------------------- 标识符与参数

    def placeholder(self) -> str:
        """PyMySQL 自报 `pyformat`，对应 SQL 里的 `%s`。"""
        return driver_placeholder(self.import_driver, default="%s")

    def bind(self, sql: str, count: int) -> str:
        """把 `%s` 改写成驱动真实占位符（PyMySQL 是恒等变换）。"""
        return rewrite_placeholders(sql, "%s", self.placeholder())

    # ------------------------------------------------------- 行归一

    def normalize_row(
        self, row: Any, description: Sequence[Any] | None
    ) -> Mapping[str, Any] | None:
        """归一一行；`row` 为 None 时返回 None。

        必须能把"没有行"（`fetchone()` 到游标末尾）与"一行空字典"区分开，
        否则调用方的 `if row is None` 永远不成立。
        """
        return mapping_from_row(row, description, self.column_names(description))

    def column_names(self, description: Sequence[Any] | None) -> list[str]:
        return [str(item[0]) for item in (description or ())]

    def coerce_value(self, value: Any) -> Any:
        return coerce_jsonable(value)

    # ---------------------------------------------------------- 类型

    def type_family(self, data_type: str, column_type: str = "") -> TypeFamily:
        dt = (data_type or "").strip().lower()
        # tinyint(1) 是 MySQL 里事实上的布尔列。
        if dt == "tinyint" and "tinyint(1)" in (column_type or "").replace(" ", "").lower():
            return TypeFamily.BOOLEAN
        if dt == "bit":
            return TypeFamily.BOOLEAN
        if dt in _JSON_TYPES:
            return TypeFamily.JSON
        if dt in _DATETIME_TYPES:
            return TypeFamily.DATETIME
        if dt in _NUMERIC_TYPES:
            return TypeFamily.NUMERIC
        if dt in _STRING_TYPES:
            return TypeFamily.STRING
        if dt in _BINARY_TYPES:
            return TypeFamily.BINARY
        return TypeFamily.OTHER

    # ------------------------------------------------------ 内省 SQL

    def system_table_predicate(self, column: str = "TABLE_NAME") -> str:
        """MySQL 没有达梦那套 `##%` / `SREF%` 元数据表，无需过滤。"""
        return "1=1"

    def sql_list_tables(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            f"""
            SELECT TABLE_NAME AS table_name,
                   TABLE_TYPE AS table_type,
                   COALESCE(TABLE_ROWS, 0) AS table_rows,
                   COALESCE(TABLE_COMMENT, '') AS table_comment
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_TYPE = 'BASE TABLE'
              AND {self.system_table_predicate("TABLE_NAME")}
            ORDER BY TABLE_NAME
            """
        )

    def sql_list_columns(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            """
            SELECT c.TABLE_NAME AS table_name,
                   c.COLUMN_NAME AS column_name,
                   c.DATA_TYPE AS data_type,
                   c.COLUMN_TYPE AS column_type,
                   c.IS_NULLABLE AS is_nullable,
                   COALESCE(c.COLUMN_KEY, '') AS column_key,
                   c.COLUMN_DEFAULT AS column_default,
                   COALESCE(c.COLUMN_COMMENT, '') AS column_comment,
                   c.ORDINAL_POSITION AS ordinal_position
            FROM information_schema.COLUMNS c
            JOIN information_schema.TABLES t
              ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
             AND t.TABLE_TYPE = 'BASE TABLE'
            WHERE c.TABLE_SCHEMA = DATABASE()
            ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
            """
        )

    def sql_list_foreign_keys(self, scope: CatalogScope) -> BoundSQL:
        """外键。

        历史实现先查一遍 KEY_COLUMN_USAGE，再**每条外键单独查一次**
        REFERENTIAL_CONSTRAINTS（N+1），而且按约束名去重导致复合外键
        只剩第一列。这里一次 JOIN 拿全，并保留每一列。
        """
        return BoundSQL(
            """
            SELECT k.CONSTRAINT_NAME AS constraint_name,
                   k.TABLE_NAME AS table_name,
                   k.COLUMN_NAME AS column_name,
                   k.REFERENCED_TABLE_NAME AS ref_table_name,
                   k.REFERENCED_COLUMN_NAME AS ref_column_name,
                   COALESCE(r.DELETE_RULE, '') AS delete_rule,
                   COALESCE(r.UPDATE_RULE, '') AS update_rule,
                   k.ORDINAL_POSITION AS ordinal_position
            FROM information_schema.KEY_COLUMN_USAGE k
            LEFT JOIN information_schema.REFERENTIAL_CONSTRAINTS r
              ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
             AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME
             AND r.TABLE_NAME = k.TABLE_NAME
            WHERE k.TABLE_SCHEMA = DATABASE()
              AND k.REFERENCED_TABLE_NAME IS NOT NULL
            ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION
            """
        )

    def sql_list_indexes(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            """
            SELECT s.TABLE_NAME AS table_name,
                   s.INDEX_NAME AS index_name,
                   s.COLUMN_NAME AS column_name,
                   CASE WHEN s.NON_UNIQUE = 0 THEN 1 ELSE 0 END AS is_unique,
                   CASE WHEN s.INDEX_NAME = 'PRIMARY' THEN 1 ELSE 0 END AS is_primary,
                   s.SEQ_IN_INDEX AS ordinal_position
            FROM information_schema.STATISTICS s
            WHERE s.TABLE_SCHEMA = DATABASE()
            ORDER BY s.TABLE_NAME, s.INDEX_NAME, s.SEQ_IN_INDEX
            """
        )

    def sql_row_count(self, table: str, mode: RowCountMode = RowCountMode.EXACT) -> BoundSQL:
        schema, name = split_qualified_name(table)
        if mode is RowCountMode.APPROX:
            return BoundSQL(
                """
                SELECT COALESCE(TABLE_ROWS, 0) AS row_count
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s
                """,
                (schema, name),
            )
        return BoundSQL(f"SELECT COUNT(*) AS row_count FROM {self.qualified(schema, name)}")

    def sql_sample(self, table: str, columns: Sequence[str], n: int) -> BoundSQL:
        schema, name = split_qualified_name(table)
        projection = ", ".join(self.quote(c) for c in columns) if columns else "*"
        return BoundSQL(
            f"SELECT {projection} FROM {self.qualified(schema, name)} {self.limit_clause(n)}"
        )

    def sql_count_tables(self, scope: CatalogScope) -> BoundSQL:
        """连接自检用：只数表，不取明细。"""
        return BoundSQL(
            f"""
            SELECT COUNT(*) AS table_count
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_TYPE = 'BASE TABLE'
              AND {self.system_table_predicate("TABLE_NAME")}
            """
        )

    def sql_set_statement_timeout(self, ms: int) -> BoundSQL | None:
        """MySQL 用 `MAX_EXECUTION_TIME`（毫秒，仅 SELECT 生效）。"""
        return BoundSQL("SET SESSION MAX_EXECUTION_TIME = %s", (int(ms),))

    def sql_checksum(self, scope: CatalogScope) -> BoundSQL:
        """结构新鲜度检查点。

        **已知局限**：取的 `COALESCE(UPDATE_TIME, CREATE_TIME)` 在 InnoDB 上
        常年为 NULL（后台统计线程不刷新），所以它只能感知建表/改表，
        感知不到列级 DDL 之外的多数变化；行数变化也完全看不到。
        因此上层必须同时提供"强制刷新"入口，不能只依赖它。
        """
        return BoundSQL(
            """
            SELECT TABLE_NAME AS object_name,
                   CAST(COALESCE(UPDATE_TIME, CREATE_TIME) AS CHAR) AS fingerprint
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
            ORDER BY TABLE_NAME
            """
        )

    def checksum_is_reliable(self) -> bool:
        return False

    # ------------------------------------------------------- 提示词

    def prompt_notes(self) -> tuple[str, ...]:
        return (
            "表名和列名请用反引号包裹，例如 `employee`。",
            "限制返回行数请用 LIMIT n（MySQL 不支持 FETCH FIRST / ROWNUM）。",
            "字符串拼接用 CONCAT(a, b)，不支持 ||。",
            "日期处理用 CURDATE() / NOW() / DATE_SUB(NOW(), INTERVAL 7 DAY) / "
            "DATE_FORMAT(col, '%Y-%m')。",
            "取当前库的表清单可查 information_schema.TABLES；MySQL 支持 SHOW TABLES 与 DESCRIBE 表名。",
            "MySQL 里空串 '' 与 NULL 是两个不同的值，判空用 IS NULL。",
        )
