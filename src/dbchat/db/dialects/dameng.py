"""达梦(Dameng)方言。

达梦兼容 Oracle 的数据字典，因此内省 SQL 走 `ALL_TABLES` /
`ALL_TAB_COLUMNS` / `ALL_CONSTRAINTS` 这套 `ALL_*` 视图，而不是
`information_schema`。几处必须记住的差异：

* **`ALL_TABLES.NUM_ROWS` 未收集统计信息时整片为 0**。本地容器实测
  16 张表行数全为 0，所以 `capabilities.approx_row_count_reliable`
  为假，内省会对 0 行的表补 `COUNT(*)`（`sql_row_count(EXACT)`）。
* **不支持 `LIMIT`**，所以 `row_limit_style` 是 `FETCH_FIRST`，
  `limit_clause()` 产出 `FETCH FIRST n ROWS ONLY`。
* **元数据自带 `##%` / `SREF%` 表**（达梦内部对象）。这个魔法过滤
  只出现在 `system_table_predicate` 一处 —— 历史实现把它抄在了
  schema_cache 的两个分支和 tools.py 里，三份还不完全一致
  （列清单那处漏了 `SREF%`）。
* **未加引号的标识符折叠成大写**，所以内省结果的键通常是大写；
  行映射大小写不敏感，上层照常用小写取值。
"""

from __future__ import annotations

import logging
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
from dbchat.db.drivers import (
    classify_dmpython,
    driver_placeholder,
    load_dmpython,
    log_ping_failure,
)
from dbchat.db.sqltext import rewrite_placeholders, split_qualified_name
from dbchat.domain.errors import DB_ERROR_MESSAGES, DbError, DbErrorKind
from dbchat.domain.profile import DatabaseProfile, DialectKind
from dbchat.domain.query import BoundSQL, TypeFamily
from dbchat.domain.schema import CatalogScope

logger = logging.getLogger(__name__)

#: 达梦内部对象前缀（`##` 系列为系统表，`SREF` 为全文检索相关对象）。
_INTERNAL_TABLE_PREFIXES = ("##%", "SREF%")

_NUMERIC_TYPES = frozenset(
    {
        "int", "integer", "bigint", "smallint", "tinyint", "byte",
        "number", "numeric", "decimal", "dec", "float", "double",
        "double precision", "real", "bit", "binary_double", "binary_float",
    }
)
_DATETIME_TYPES = frozenset({"date", "datetime", "time", "timestamp", "timestamp with time zone"})
_STRING_TYPES = frozenset(
    {
        "char", "character", "varchar", "varchar2", "nvarchar", "nvarchar2",
        "nchar", "text", "clob", "nclob", "longvarchar", "long", "varchar_cn",
    }
)
_BINARY_TYPES = frozenset({"binary", "varbinary", "blob", "image", "bfile", "varbyte", "longvarbinary"})


@register_dialect
class DamengDialect(Dialect):
    """达梦 8 方言（Oracle 兼容模式）。"""

    kind = DialectKind.DAMENG
    display_name = "达梦(Dameng)"
    sqlglot_name = "oracle"
    driver_package = "dmPython"
    identifier_quote = '"'
    row_limit_style = RowLimitStyle.FETCH_FIRST

    capabilities = DialectCapabilities(
        supports_show=False,
        supports_describe=False,
        supports_explain=True,
        supports_tablesample=False,
        supports_schemas=True,
        supports_comments=True,
        # 实测：本地库未收集统计信息时 NUM_ROWS 全为 0。
        approx_row_count_reliable=False,
        table_mtime_reliable=False,
    )

    system_schemas = frozenset(
        {"SYS", "SYSDBA", "SYSAUDITOR", "SYSSSO", "SYSJOB", "CTISYS", "SYSTEM"}
    )
    #: MySQL 命令 + Oracle 系维护命令都不认。
    disallowed_leading_keywords = frozenset(
        {"SHOW", "DESCRIBE", "DESC", "HANDLER", "TABLE", "VALUES", "VACUUM", "REINDEX", "ANALYZE"}
    )
    #: 达梦只支持这几个只读首关键字（`TABLE t` / `VALUES (...)` 是 MySQL/PG 语法）。
    allowed_leading_keywords = frozenset({"SELECT", "WITH", "EXPLAIN"})

    # ------------------------------------------------------------ 连接

    def import_driver(self) -> Any:
        return load_dmpython()

    def connect(self, profile: DatabaseProfile, *, timeout_s: int = 30) -> Any:
        dm_python = self.import_driver()
        try:
            conn = dm_python.connect(
                server=profile.host,
                port=profile.port,
                user=profile.user,
                password=profile.password,
                login_timeout=timeout_s,
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

        self._select_schema(conn, profile)
        return conn

    def _select_schema(self, conn: Any, profile: DatabaseProfile) -> None:
        """把会话切到 `profile.database` 指定的 schema。

        `SET SCHEMA` 不能参数化（Oracle 系语法），因此标识符必须经过
        `quote()` 转义后再拼接。切换失败**不致命**：目标 schema 可能是
        库名而不是 schema 名（本地容器即如此，schema 实际是 SYSDBA），
        此时保留驱动给的当前 schema 并记 warning，绝不静默吞掉。
        """
        target = self.default_schema(profile)
        if not target:
            return
        cur = None
        try:
            cur = conn.cursor()
            cur.execute(f"SET SCHEMA {self.quote(target)}")
        except Exception as exc:
            logger.warning(
                "切换到 schema %s 失败，继续使用当前 schema %s: %s",
                target,
                getattr(conn, "current_schema", "?"),
                exc,
            )
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception as exc:  # pragma: no cover - 关闭失败不影响连接
                    logger.debug("关闭 SET SCHEMA 游标失败: %s", exc)

    def ping(self, conn: Any) -> bool:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM DUAL")
            cur.close()
            return True
        except Exception as exc:
            log_ping_failure(conn, exc)
            return False

    def create_cursor(self, conn: Any) -> Any:
        """达梦返回元组行，形状由 `normalize_row` 按 description 归一。"""
        return conn.cursor()

    def classify_error(self, exc: BaseException) -> DbErrorKind:
        return classify_dmpython(exc)

    # -------------------------------------------------- 标识符与参数

    def placeholder(self) -> str:
        """dmPython 自报 `qmark`（实测），对应 SQL 里的 `?`。"""
        return driver_placeholder(self.import_driver, default="?")

    def bind(self, sql: str, count: int) -> str:
        """把 `%s` 改写成 `?`，字面量里的 `%s` 不动。

        历史实现是 `sql.replace("%s", "?")` —— `WHERE note LIKE '%s%'`
        会被改成 `LIKE '?%'`，查询结果直接错。
        """
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
        """达梦未加引号的列名/别名一律是大写，这里统一成大写。"""
        return [str(item[0]).upper() for item in (description or ())]

    def coerce_value(self, value: Any) -> Any:
        coerced = coerce_jsonable(value)
        if coerced is not value:
            return coerced
        # 达梦的 LOB 对象：读出来再当文本处理
        reader = getattr(value, "read", None)
        if callable(reader):
            try:
                return coerce_jsonable(reader())
            except Exception as exc:
                logger.warning("读取达梦 LOB 值失败，退回字符串: %s", exc)
                return str(value)
        return value

    # ---------------------------------------------------------- 类型

    def type_family(self, data_type: str, column_type: str = "") -> TypeFamily:
        dt = (data_type or "").strip().lower()
        if dt in ("bit", "boolean", "bool"):
            return TypeFamily.BOOLEAN
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
        """排除达梦内部表。

        全项目**唯一**的一处 `##%` / `SREF%` 魔法过滤；所有用到的地方
        （表清单、列清单、行数、抽样）都从这里取。
        """
        return " AND ".join(f"{column} NOT LIKE '{prefix}'" for prefix in _INTERNAL_TABLE_PREFIXES)

    def sql_list_tables(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            f"""
            SELECT t.TABLE_NAME AS table_name,
                   'BASE TABLE' AS table_type,
                   COALESCE(t.NUM_ROWS, 0) AS table_rows,
                   COALESCE((SELECT COMMENTS FROM ALL_TAB_COMMENTS
                              WHERE TABLE_NAME = t.TABLE_NAME AND OWNER = t.OWNER), '')
                       AS table_comment
            FROM ALL_TABLES t
            WHERE t.OWNER = USER
              AND {self.system_table_predicate("t.TABLE_NAME")}
            ORDER BY t.TABLE_NAME
            """
        )

    def sql_list_columns(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            f"""
            SELECT c.TABLE_NAME AS table_name,
                   c.COLUMN_NAME AS column_name,
                   c.DATA_TYPE AS data_type,
                   c.DATA_TYPE || '(' || c.DATA_LENGTH || ')' AS column_type,
                   c.NULLABLE AS is_nullable,
                   CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'PRI' ELSE '' END AS column_key,
                   c.DATA_DEFAULT AS column_default,
                   COALESCE(cm.COMMENTS, '') AS column_comment,
                   c.COLUMN_ID AS ordinal_position
            FROM ALL_TAB_COLUMNS c
            LEFT JOIN ALL_COL_COMMENTS cm
                   ON cm.TABLE_NAME = c.TABLE_NAME
                  AND cm.COLUMN_NAME = c.COLUMN_NAME
                  AND cm.OWNER = c.OWNER
            LEFT JOIN (
                SELECT acc.TABLE_NAME, acc.COLUMN_NAME
                FROM ALL_CONS_COLUMNS acc
                JOIN ALL_CONSTRAINTS ac
                  ON ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME AND ac.OWNER = acc.OWNER
                WHERE ac.CONSTRAINT_TYPE = 'P' AND acc.OWNER = USER
            ) pk ON pk.TABLE_NAME = c.TABLE_NAME AND pk.COLUMN_NAME = c.COLUMN_NAME
            WHERE c.OWNER = USER
              AND {self.system_table_predicate("c.TABLE_NAME")}
            ORDER BY c.TABLE_NAME, c.COLUMN_ID
            """
        )

    def sql_list_foreign_keys(self, scope: CatalogScope) -> BoundSQL:
        """外键。

        `acc_r` 必须按 `POSITION` 与被引用列对齐 —— 历史实现只按约束名
        连接，复合外键会退化成两列交叉相乘。
        """
        return BoundSQL(
            f"""
            SELECT ac.CONSTRAINT_NAME AS constraint_name,
                   ac.TABLE_NAME AS table_name,
                   acc.COLUMN_NAME AS column_name,
                   ac_r.TABLE_NAME AS ref_table_name,
                   acc_r.COLUMN_NAME AS ref_column_name,
                   COALESCE(ac.DELETE_RULE, '') AS delete_rule,
                   '' AS update_rule,
                   acc.POSITION AS ordinal_position
            FROM ALL_CONSTRAINTS ac
            JOIN ALL_CONS_COLUMNS acc
              ON acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME AND acc.OWNER = ac.OWNER
            LEFT JOIN ALL_CONSTRAINTS ac_r
              ON ac_r.CONSTRAINT_NAME = ac.R_CONSTRAINT_NAME AND ac_r.OWNER = ac.R_OWNER
            LEFT JOIN ALL_CONS_COLUMNS acc_r
              ON acc_r.CONSTRAINT_NAME = ac_r.CONSTRAINT_NAME
             AND acc_r.OWNER = ac_r.OWNER
             AND acc_r.POSITION = acc.POSITION
            WHERE ac.CONSTRAINT_TYPE = 'R' AND ac.OWNER = USER
              AND {self.system_table_predicate("ac.TABLE_NAME")}
            ORDER BY ac.TABLE_NAME, ac.CONSTRAINT_NAME, acc.POSITION
            """
        )

    def sql_list_indexes(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            f"""
            SELECT ai.TABLE_NAME AS table_name,
                   ai.INDEX_NAME AS index_name,
                   aic.COLUMN_NAME AS column_name,
                   CASE WHEN ai.UNIQUENESS = 'UNIQUE' THEN 1 ELSE 0 END AS is_unique,
                   CASE WHEN pk.CONSTRAINT_NAME IS NOT NULL THEN 1 ELSE 0 END AS is_primary,
                   aic.COLUMN_POSITION AS ordinal_position
            FROM ALL_INDEXES ai
            JOIN ALL_IND_COLUMNS aic
              ON aic.INDEX_NAME = ai.INDEX_NAME AND aic.INDEX_OWNER = ai.OWNER
            LEFT JOIN ALL_CONSTRAINTS pk
              ON pk.INDEX_NAME = ai.INDEX_NAME
             AND pk.OWNER = ai.OWNER
             AND pk.CONSTRAINT_TYPE = 'P'
            WHERE ai.TABLE_OWNER = USER
              AND {self.system_table_predicate("ai.TABLE_NAME")}
            ORDER BY ai.TABLE_NAME, ai.INDEX_NAME, aic.COLUMN_POSITION
            """
        )

    def sql_row_count(self, table: str, mode: RowCountMode = RowCountMode.EXACT) -> BoundSQL:
        schema, name = split_qualified_name(table)
        if mode is RowCountMode.APPROX:
            return BoundSQL(
                """
                SELECT COALESCE(NUM_ROWS, 0) AS row_count
                FROM ALL_TABLES
                WHERE OWNER = USER AND TABLE_NAME = %s
                """,
                (name,),
            )
        return BoundSQL(f"SELECT COUNT(*) AS row_count FROM {self.qualified(schema, name)}")

    def sql_sample(self, table: str, columns: Sequence[str], n: int) -> BoundSQL:
        schema, name = split_qualified_name(table)
        projection = ", ".join(self.quote(c) for c in columns) if columns else "*"
        # 达梦不认 LIMIT，这里必然是 FETCH FIRST n ROWS ONLY。
        return BoundSQL(
            f"SELECT {projection} FROM {self.qualified(schema, name)} {self.limit_clause(n)}"
        )

    def sql_count_tables(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            f"""
            SELECT COUNT(*) AS table_count
            FROM ALL_TABLES t
            WHERE t.OWNER = USER
              AND {self.system_table_predicate("t.TABLE_NAME")}
            """
        )

    def sql_set_statement_timeout(self, ms: int) -> BoundSQL | None:
        """达梦没有会话级语句超时，交由上层的整体预算兜底。"""
        return None

    def sql_checksum(self, scope: CatalogScope) -> BoundSQL:
        """结构新鲜度检查点：行数统计 + 最近 DDL 时间。

        **已知局限**：`NUM_ROWS` 未收集统计信息时恒为 0（本地容器即是），
        `LAST_DDL_TIME` 只覆盖 DDL 不覆盖 DML。两者都只是"廉价提示"，
        不能作为"结构未变"的证明，上层必须提供强制刷新入口。
        """
        return BoundSQL(
            f"""
            SELECT t.TABLE_NAME AS object_name,
                   CAST(t.NUM_ROWS AS VARCHAR(64)) || ':' || COALESCE(o.LAST_DDL_TIME, '') AS fingerprint
            FROM ALL_TABLES t
            LEFT JOIN ALL_OBJECTS o
                   ON o.OBJECT_NAME = t.TABLE_NAME AND o.OWNER = t.OWNER AND o.OBJECT_TYPE = 'TABLE'
            WHERE t.OWNER = USER
              AND {self.system_table_predicate("t.TABLE_NAME")}
            ORDER BY t.TABLE_NAME
            """
        )

    def checksum_is_reliable(self) -> bool:
        return False

    # ------------------------------------------------------- 提示词

    def prompt_notes(self) -> tuple[str, ...]:
        return (
            '表名和列名请用双引号包裹，例如 "EMPLOYEE"；达梦未加引号时折叠为大写。',
            "限制返回行数请用 FETCH FIRST n ROWS ONLY —— 达梦不支持 LIMIT。",
            "字符串拼接用 ||，也可以用 CONCAT()。",
            "日期处理用 SYSDATE、TO_DATE('2024-01-01','YYYY-MM-DD')、TO_CHAR(col,'YYYY-MM')。",
            "空值判断用 IS NULL，取默认值用 NVL(col, 0)；达梦里 '' 等价于 NULL。",
            "达梦不支持 SHOW / DESCRIBE，也没有 information_schema；查表结构请查 "
            "ALL_TABLES / ALL_TAB_COLUMNS / ALL_CONSTRAINTS（OWNER = USER）。",
            "当前会话的 schema 用 USER 或 SYS_CONTEXT('USERENV','CURRENT_SCHEMA') 表示。",
        )
