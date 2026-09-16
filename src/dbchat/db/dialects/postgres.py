"""PostgreSQL 方言。

与历史实现（`app/database/schema_cache.py` 的 `is_pg` 分支）的差异：

* 表清单的 `pg_class` 连接补上了命名空间条件 —— 旧写法只按 `relname`
  连接，同名表出现在多个 schema 时会把行数放大。
* 外键改用 `pg_constraint` + `unnest(conkey, confkey) WITH ORDINALITY`：
  旧写法经 `information_schema.CONSTRAINT_COLUMN_USAGE` 连接，复合外键
  会退化成笛卡尔积，且删除/更新规则取自另一张表、列与列对不上号。
* 索引改用 `pg_index` + `pg_get_indexdef`，能给出真实的索引列，
  旧写法只能从 `pg_indexes.indexdef` 里"看到 UNIQUE 字样"而拿不到列。

占位符一律写 `%s`（本模块内省 SQL 的契约），psycopg2 自身即 `pyformat`，
`bind()` 是恒等变换。
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
from dbchat.db.drivers import classify_psycopg2, driver_placeholder, load_psycopg2, log_ping_failure
from dbchat.db.sqltext import rewrite_placeholders, split_qualified_name
from dbchat.domain.errors import DB_ERROR_MESSAGES, DbError, DbErrorKind
from dbchat.domain.profile import DatabaseProfile, DialectKind
from dbchat.domain.query import BoundSQL, TypeFamily
from dbchat.domain.schema import CatalogScope

#: 系统 schema —— 内省 SQL 与校验共用同一份定义。
_SYSTEM_SCHEMAS_SQL = "'pg_catalog', 'information_schema', 'pg_toast'"

_NUMERIC_TYPES = frozenset(
    {
        "smallint", "integer", "bigint", "int2", "int4", "int8", "smallserial",
        "serial", "bigserial", "decimal", "numeric", "real", "double precision",
        "float4", "float8", "money", "oid",
    }
)
_DATETIME_TYPES = frozenset(
    {"timestamp", "timestamp without time zone", "timestamp with time zone",
     "timestamptz", "date", "time", "time without time zone", "time with time zone",
     "timetz", "interval"}
)
_STRING_TYPES = frozenset(
    {"character varying", "varchar", "character", "char", "bpchar", "text", "name",
     "citext", "uuid", "inet", "cidr", "macaddr"}
)
_BINARY_TYPES = frozenset({"bytea"})
_JSON_TYPES = frozenset({"json", "jsonb", "xml"})


@register_dialect
class PostgresDialect(Dialect):
    """PostgreSQL 12+ 方言。"""

    kind = DialectKind.POSTGRESQL
    display_name = "PostgreSQL"
    sqlglot_name = "postgres"
    driver_package = "psycopg2-binary"
    identifier_quote = '"'
    row_limit_style = RowLimitStyle.LIMIT

    capabilities = DialectCapabilities(
        supports_show=False,
        supports_describe=False,
        supports_explain=True,
        supports_tablesample=True,
        supports_schemas=True,
        supports_comments=True,
        # pg_stat_user_tables.n_live_tup 是统计信息，但 PG 默认开着 autovacuum，
        # 且 reltuples 在空库上就是准确的。
        approx_row_count_reliable=True,
        table_mtime_reliable=False,
    )

    system_schemas = frozenset({"pg_catalog", "information_schema", "pg_toast"})
    #: SHOW / DESCRIBE 是 MySQL 命令。
    disallowed_leading_keywords = frozenset({"SHOW", "DESCRIBE", "DESC", "HANDLER"})

    # ------------------------------------------------------------ 连接

    def import_driver(self) -> Any:
        return load_psycopg2()

    def connect(self, profile: DatabaseProfile, *, timeout_s: int = 30) -> Any:
        psycopg2 = self.import_driver()
        try:
            conn = psycopg2.connect(
                host=profile.host,
                port=profile.port,
                user=profile.user,
                password=profile.password,
                dbname=profile.database or None,
                connect_timeout=timeout_s,
            )
            conn.autocommit = True
            return conn
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
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return True
        except Exception as exc:
            log_ping_failure(conn, exc)
            return False

    def create_cursor(self, conn: Any) -> Any:
        psycopg2 = self.import_driver()
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def classify_error(self, exc: BaseException) -> DbErrorKind:
        return classify_psycopg2(exc)

    # -------------------------------------------------- 标识符与参数

    def placeholder(self) -> str:
        """psycopg2 自报 `pyformat`，对应 SQL 里的 `%s`。"""
        return driver_placeholder(self.import_driver, default="%s")

    def bind(self, sql: str, count: int) -> str:
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
        return [str(getattr(item, "name", item[0])) for item in (description or ())]

    def coerce_value(self, value: Any) -> Any:
        return coerce_jsonable(value)

    # ---------------------------------------------------------- 类型

    def type_family(self, data_type: str, column_type: str = "") -> TypeFamily:
        dt = (data_type or "").strip().lower()
        if dt in ("boolean", "bool"):
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
        if dt.endswith("[]"):
            return TypeFamily.OTHER  # 数组列：不能直接聚合
        return TypeFamily.OTHER

    # ------------------------------------------------------ 内省 SQL

    def tablesample_clause(self, percent: int) -> str:
        """大表抽样子句。

        `capabilities.supports_tablesample` 为真时，探索层可在
        `sql_sample` 之后追加该子句（`TABLESAMPLE SYSTEM(n)`），
        避免对亿级表做全表扫描。
        """
        return f"TABLESAMPLE SYSTEM({int(percent)})"

    def sql_list_tables(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            f"""
            SELECT t.tablename AS table_name,
                   'BASE TABLE' AS table_type,
                   COALESCE(s.n_live_tup, 0) AS table_rows,
                   COALESCE(pg_catalog.obj_description(pc.oid), '') AS table_comment
            FROM pg_catalog.pg_tables t
            LEFT JOIN pg_catalog.pg_class pc
                   ON pc.relname = t.tablename
                  AND pc.relnamespace = (
                        SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = t.schemaname
                      )
            LEFT JOIN pg_catalog.pg_stat_user_tables s
                   ON s.relname = t.tablename AND s.schemaname = t.schemaname
            WHERE t.schemaname NOT IN ({_SYSTEM_SCHEMAS_SQL})
            ORDER BY t.tablename
            """
        )

    def sql_list_columns(self, scope: CatalogScope) -> BoundSQL:
        """列清单。

        主键/唯一键用**聚合子查询**取，保证"一列一行"；旧实现直接连接
        `KEY_COLUMN_USAGE`，一个列同时属于主键和唯一键时会把列复制成多行。
        """
        return BoundSQL(
            f"""
            SELECT c.table_name AS table_name,
                   c.column_name AS column_name,
                   c.data_type AS data_type,
                   CASE WHEN c.character_maximum_length IS NOT NULL
                        THEN c.data_type || '(' || c.character_maximum_length || ')'
                        WHEN c.numeric_precision IS NOT NULL
                        THEN c.data_type || '(' || c.numeric_precision || ','
                             || COALESCE(c.numeric_scale, 0) || ')'
                        ELSE c.data_type END AS column_type,
                   c.is_nullable AS is_nullable,
                   COALESCE(k.kind, '') AS column_key,
                   c.column_default AS column_default,
                   COALESCE(pg_catalog.col_description(pc.oid, c.ordinal_position), '')
                       AS column_comment,
                   c.ordinal_position AS ordinal_position
            FROM information_schema.columns c
            LEFT JOIN pg_catalog.pg_class pc
                   ON pc.relname = c.table_name
                  AND pc.relnamespace = (
                        SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = c.table_schema
                      )
            LEFT JOIN (
                SELECT kcu.table_schema, kcu.table_name, kcu.column_name,
                       MIN(CASE WHEN tc.constraint_type = 'PRIMARY KEY' THEN 'PRI'
                                WHEN tc.constraint_type = 'UNIQUE' THEN 'UNI'
                                ELSE '' END) AS kind
                FROM information_schema.key_column_usage kcu
                JOIN information_schema.table_constraints tc
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
                GROUP BY kcu.table_schema, kcu.table_name, kcu.column_name
            ) k ON k.table_schema = c.table_schema
               AND k.table_name = c.table_name
               AND k.column_name = c.column_name
            WHERE c.table_schema NOT IN ({_SYSTEM_SCHEMAS_SQL})
            ORDER BY c.table_name, c.ordinal_position
            """
        )

    def sql_list_foreign_keys(self, scope: CatalogScope) -> BoundSQL:
        """外键：按列位置取出，复合外键不会被摊成笛卡尔积。"""
        return BoundSQL(
            f"""
            SELECT con.conname AS constraint_name,
                   src.relname AS table_name,
                   sa.attname AS column_name,
                   tgt.relname AS ref_table_name,
                   ta.attname AS ref_column_name,
                   CASE con.confdeltype
                        WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
                        WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL'
                        WHEN 'd' THEN 'SET DEFAULT' ELSE '' END AS delete_rule,
                   CASE con.confupdtype
                        WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT'
                        WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL'
                        WHEN 'd' THEN 'SET DEFAULT' ELSE '' END AS update_rule,
                   k.ordinality AS ordinal_position
            FROM pg_catalog.pg_constraint con
            JOIN pg_catalog.pg_class src ON src.oid = con.conrelid
            JOIN pg_catalog.pg_namespace ns ON ns.oid = src.relnamespace
            JOIN pg_catalog.pg_class tgt ON tgt.oid = con.confrelid
            JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY
                 AS k(src_attnum, tgt_attnum, ordinality) ON TRUE
            JOIN pg_catalog.pg_attribute sa
              ON sa.attrelid = con.conrelid AND sa.attnum = k.src_attnum
            JOIN pg_catalog.pg_attribute ta
              ON ta.attrelid = con.confrelid AND ta.attnum = k.tgt_attnum
            WHERE con.contype = 'f' AND ns.nspname NOT IN ({_SYSTEM_SCHEMAS_SQL})
            ORDER BY src.relname, con.conname, k.ordinality
            """
        )

    def sql_list_indexes(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            f"""
            SELECT t.relname AS table_name,
                   i.relname AS index_name,
                   pg_catalog.pg_get_indexdef(i.oid, k.position, TRUE) AS column_name,
                   CASE WHEN ix.indisunique THEN 1 ELSE 0 END AS is_unique,
                   CASE WHEN ix.indisprimary THEN 1 ELSE 0 END AS is_primary,
                   k.position AS ordinal_position
            FROM pg_catalog.pg_class t
            JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_catalog.pg_index ix ON ix.indrelid = t.oid
            JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
            JOIN LATERAL generate_series(1, GREATEST(ix.indnkeyatts, 1))
                 AS k(position) ON TRUE
            WHERE t.relkind IN ('r', 'p')
              AND n.nspname NOT IN ({_SYSTEM_SCHEMAS_SQL})
            ORDER BY t.relname, i.relname, k.position
            """
        )

    def sql_row_count(self, table: str, mode: RowCountMode = RowCountMode.EXACT) -> BoundSQL:
        schema, name = split_qualified_name(table)
        if mode is RowCountMode.APPROX:
            return BoundSQL(
                """
                SELECT COALESCE(c.reltuples, 0)::bigint AS row_count
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = %s AND n.nspname = COALESCE(%s, current_schema())
                """,
                (name, schema),
            )
        return BoundSQL(f"SELECT COUNT(*) AS row_count FROM {self.qualified(schema, name)}")

    def sql_sample(self, table: str, columns: Sequence[str], n: int) -> BoundSQL:
        schema, name = split_qualified_name(table)
        projection = ", ".join(self.quote(c) for c in columns) if columns else "*"
        return BoundSQL(
            f"SELECT {projection} FROM {self.qualified(schema, name)} {self.limit_clause(n)}"
        )

    def sql_count_tables(self, scope: CatalogScope) -> BoundSQL:
        return BoundSQL(
            f"""
            SELECT COUNT(*) AS table_count
            FROM pg_catalog.pg_tables
            WHERE schemaname NOT IN ({_SYSTEM_SCHEMAS_SQL})
            """
        )

    def sql_set_statement_timeout(self, ms: int) -> BoundSQL | None:
        """会话级语句超时。

        历史实现用的是 `SET LOCAL statement_timeout`，但在 autocommit 下
        每条语句自成事务，`SET LOCAL` 只作用于它自己，等于没设。
        """
        return BoundSQL("SET statement_timeout = %s", (int(ms),))

    def sql_checksum(self, scope: CatalogScope) -> BoundSQL:
        """结构新鲜度检查点：表文件大小。

        **已知局限**：`pg_relation_size` 只在表文件增长时变化 ——
        改列、加索引、删数据、原地更新都可能完全看不出来。
        所以它只能当"廉价提示"，不能当"结构未变"的证明；
        上层必须提供强制刷新入口。
        """
        return BoundSQL(
            f"""
            SELECT c.relname AS object_name,
                   pg_catalog.pg_relation_size(c.oid)::text AS fingerprint
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ({_SYSTEM_SCHEMAS_SQL})
            ORDER BY c.relname
            """
        )

    def checksum_is_reliable(self) -> bool:
        return False

    # ------------------------------------------------------- 提示词

    def prompt_notes(self) -> tuple[str, ...]:
        return (
            '表名和列名请用双引号包裹，例如 "employee"；未加引号时会被折叠成小写。',
            "限制返回行数请用 LIMIT n（不支持 FETCH FIRST / ROWNUM）。",
            "字符串拼接用 ||，也可以用 CONCAT()。",
            "日期处理用 now() - INTERVAL '7 day'、date_trunc('month', col)、"
            "col::date、to_char(col, 'YYYY-MM')。",
            "大小写不敏感匹配用 ILIKE，正则匹配用 ~ / ~*。",
            "PostgreSQL 不支持 SHOW / DESCRIBE；查表结构请查 information_schema.tables / columns。",
            "大表抽样可用 TABLESAMPLE SYSTEM(n)。",
        )
