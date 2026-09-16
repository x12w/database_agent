"""方言矩阵 —— 一张表驱动三个方言的全部可观察行为。

**不需要任何数据库**：这里只验证"方言对象说了什么"，不验证"数据库答了什么"。
真库验证在 `tests/integration/test_live_dbs.py`。

新增一个国产库时，只要在 `CASES` 里加一行 —— 表少一行会被
`test_matrix_covers_every_registered_dialect` 抓住。
"""

from __future__ import annotations

import datetime
import decimal
from dataclasses import dataclass
from typing import Any

import pytest

from dbchat.db.dialects import get_dialect, registered_kinds
from dbchat.db.dialects.base import RowLimitStyle
from dbchat.domain.profile import DatabaseProfile, DialectKind
from dbchat.domain.query import BoundSQL, TypeFamily
from dbchat.domain.schema import CatalogScope


@dataclass(frozen=True)
class Case:
    """一个方言在矩阵里的全部期望值。"""

    kind: DialectKind
    display_name: str
    #: 标识符引用：(输入, 期望输出)，必须含内嵌引用符的转义用例
    quote_pairs: tuple[tuple[str, str], ...]
    row_limit_style: RowLimitStyle
    limit_clause: str
    placeholder: str
    #: 占位符改写：(输入, 期望输出)；必须含字面量保护用例
    bind_pairs: tuple[tuple[str, str], ...]
    #: 系统表谓词：(列名, 期望 SQL 片段)
    system_predicate: tuple[str, str]
    #: 类型归类：(data_type, column_type, 期望大类)
    type_families: tuple[tuple[str, str, TypeFamily], ...]
    #: 行归一：原始行 / 列描述 / 期望键值
    raw_row: Any
    row_description: tuple[Any, ...] | None
    expected_row: dict[str, Any]
    #: 该方言必须拒绝的首关键字
    disallowed_leading: frozenset[str]
    #: 表清单 SQL 里必须出现的方言标记
    marker_sql: str
    #: 计数 SQL 里必须出现的标记
    count_marker: str


class _Column:
    """模拟 psycopg2 的 `Column` 对象（`description` 元素带 `.name`）。"""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __getitem__(self, index: int) -> Any:  # pragma: no cover - 兜底
        return (self.name,)[index]


CASES: tuple[Case, ...] = (
    Case(
        kind=DialectKind.MYSQL,
        display_name="MySQL",
        quote_pairs=(
            ("employee", "`employee`"),
            ('we"ird', '`we"ird`'),
            ("back`tick", "`back``tick`"),
        ),
        row_limit_style=RowLimitStyle.LIMIT,
        limit_clause="LIMIT 7",
        placeholder="%s",
        bind_pairs=(
            ("SELECT * FROM t WHERE a = %s", "SELECT * FROM t WHERE a = %s"),
            ("SELECT * FROM t WHERE note LIKE '%s%'", "SELECT * FROM t WHERE note LIKE '%s%'"),
        ),
        system_predicate=("TABLE_NAME", "1=1"),
        type_families=(
            ("int", "int(11)", TypeFamily.NUMERIC),
            ("decimal", "decimal(10,2)", TypeFamily.NUMERIC),
            ("varchar", "varchar(64)", TypeFamily.STRING),
            ("tinyint", "tinyint(1)", TypeFamily.BOOLEAN),
            ("datetime", "datetime", TypeFamily.DATETIME),
            ("blob", "blob", TypeFamily.BINARY),
            ("json", "json", TypeFamily.JSON),
        ),
        raw_row={"ID": 1, "name": "张三"},
        row_description=(("ID",), ("name",)),
        expected_row={"ID": 1, "name": "张三", "id": 1, "NAME": "张三"},
        disallowed_leading=frozenset({"VACUUM"}),
        marker_sql="information_schema.TABLES",
        count_marker="information_schema.TABLES",
    ),
    Case(
        kind=DialectKind.POSTGRESQL,
        display_name="PostgreSQL",
        quote_pairs=(
            ("employee", '"employee"'),
            ('we"ird', '"we""ird"'),
            ("back`tick", '"back`tick"'),
        ),
        row_limit_style=RowLimitStyle.LIMIT,
        limit_clause="LIMIT 7",
        placeholder="%s",
        bind_pairs=(
            ("SELECT * FROM t WHERE a = %s", "SELECT * FROM t WHERE a = %s"),
            ("SELECT * FROM t WHERE note LIKE '%s%'", "SELECT * FROM t WHERE note LIKE '%s%'"),
        ),
        system_predicate=("TABLE_NAME", "1=1"),
        type_families=(
            ("integer", "integer", TypeFamily.NUMERIC),
            ("numeric", "numeric(10,2)", TypeFamily.NUMERIC),
            ("character varying", "character varying(64)", TypeFamily.STRING),
            ("boolean", "boolean", TypeFamily.BOOLEAN),
            ("timestamp without time zone", "timestamp", TypeFamily.DATETIME),
            ("bytea", "bytea", TypeFamily.BINARY),
            ("jsonb", "jsonb", TypeFamily.JSON),
        ),
        raw_row={"id": 1, "name": "张三"},
        row_description=(_Column("id"), _Column("name")),
        expected_row={"id": 1, "name": "张三", "ID": 1, "NAME": "张三"},
        disallowed_leading=frozenset({"SHOW", "DESCRIBE"}),
        marker_sql="pg_catalog",
        count_marker="pg_catalog.pg_tables",
    ),
    Case(
        kind=DialectKind.DAMENG,
        display_name="达梦(Dameng)",
        quote_pairs=(
            ("EMPLOYEE", '"EMPLOYEE"'),
            ('we"ird', '"we""ird"'),
        ),
        row_limit_style=RowLimitStyle.FETCH_FIRST,
        limit_clause="FETCH FIRST 7 ROWS ONLY",
        placeholder="?",
        bind_pairs=(
            ("SELECT * FROM t WHERE a = %s", "SELECT * FROM t WHERE a = ?"),
            # 字面量里的 %s 绝不能被改写 —— 这正是历史实现的 bug。
            ("SELECT * FROM t WHERE note LIKE '%s%'", "SELECT * FROM t WHERE note LIKE '%s%'"),
            ("SELECT '100%s' FROM DUAL WHERE a = %s", "SELECT '100%s' FROM DUAL WHERE a = ?"),
            ("-- %s 注释\nSELECT %s FROM DUAL", "-- %s 注释\nSELECT ? FROM DUAL"),
        ),
        system_predicate=(
            "TABLE_NAME",
            "TABLE_NAME NOT LIKE '##%' AND TABLE_NAME NOT LIKE 'SREF%'",
        ),
        type_families=(
            ("INT", "INT(11)", TypeFamily.NUMERIC),
            ("NUMBER", "NUMBER(10,2)", TypeFamily.NUMERIC),
            ("VARCHAR2", "VARCHAR2(64)", TypeFamily.STRING),
            ("BIT", "BIT", TypeFamily.BOOLEAN),
            ("TIMESTAMP", "TIMESTAMP", TypeFamily.DATETIME),
            ("BLOB", "BLOB", TypeFamily.BINARY),
        ),
        raw_row=(1, "张三", None),
        row_description=(("ID",), ("NAME",), ("NOTE",)),
        expected_row={"ID": 1, "NAME": "张三", "NOTE": None, "id": 1, "name": "张三"},
        disallowed_leading=frozenset({"SHOW", "DESCRIBE", "TABLE"}),
        marker_sql="ALL_TABLES",
        count_marker="ALL_TABLES",
    ),
)


def test_matrix_covers_every_registered_dialect() -> None:
    """矩阵不能漏掉任何一个已注册的方言。"""
    covered = {case.kind for case in CASES}
    assert covered == set(registered_kinds()), (
        f"方言矩阵与实际注册的方言不一致: 矩阵 {sorted(k.value for k in covered)} "
        f"vs 注册表 {sorted(k.value for k in registered_kinds())}"
    )


def test_dialect_matrix() -> None:
    """逐条比对三个方言的可观察行为。

    一个测试体、一张表：新增库只需加一行，不需要加测试函数。
    """
    scope = CatalogScope()
    for case in CASES:
        dialect = get_dialect(case.kind)
        where = f"[{case.kind.value}]"

        # ---- 身份 ----
        assert dialect.display_name == case.display_name, where
        assert dialect.driver_package, f"{where} 未声明驱动包名"
        assert dialect.sqlglot_name, f"{where} 未声明 sqlglot 方言名"
        assert dialect.identifier_quote, f"{where} 未声明标识符引用符"
        assert dialect.prompt_notes(), f"{where} prompt_notes 为空"
        assert all(note.strip() for note in dialect.prompt_notes()), f"{where} prompt_notes 有空串"
        assert dialect.sql_dialect_hint().strip(), f"{where} 方言提示词为空"

        # ---- 标识符引用（含内嵌引用符转义）----
        for raw, expected in case.quote_pairs:
            assert dialect.quote(raw) == expected, f"{where} quote({raw!r})"

        # ---- 行数限制 ----
        assert dialect.row_limit_style is case.row_limit_style, f"{where} row_limit_style"
        assert dialect.limit_clause(7) == case.limit_clause, f"{where} limit_clause"
        if case.row_limit_style is RowLimitStyle.LIMIT:
            assert "LIMIT" in case.limit_clause, f"{where} 限流子句应当是 LIMIT"
        else:
            assert "LIMIT" not in case.limit_clause, f"{where} 不该出现 LIMIT"

        # ---- 占位符与绑定 ----
        assert dialect.placeholder() == case.placeholder, f"{where} placeholder"
        for src, expected in case.bind_pairs:
            assert dialect.bind(src, 1) == expected, f"{where} bind({src!r})"

        # ---- 系统表谓词 ----
        column, expected = case.system_predicate
        assert dialect.system_table_predicate(column) == expected, f"{where} system_table_predicate"

        # ---- 类型归类 ----
        for data_type, column_type, family in case.type_families:
            got = dialect.type_family(data_type, column_type)
            assert got is family, f"{where} type_family({data_type!r}, {column_type!r}) = {got}"

        # ---- 行归一（大小写不敏感 + 元组行）----
        row = dialect.normalize_row(case.raw_row, case.row_description)
        for key, value in case.expected_row.items():
            assert row.get(key) == value, f"{where} row[{key!r}] = {row.get(key)!r}"
            assert key in row, f"{where} row 不认键 {key!r}"
        assert dialect.normalize_row(None, case.row_description) is None, where

        # ---- 校验策略 ----
        assert case.disallowed_leading <= dialect.disallowed_leading_keywords, (
            f"{where} disallowed_leading_keywords 缺少 {case.disallowed_leading}"
        )
        assert dialect.allowed_leading_keywords, f"{where} allowed_leading_keywords 为空"

        # ---- 内省 SQL 确实是这个方言的 ----
        assert case.marker_sql in dialect.sql_list_tables(scope).sql, f"{where} 表清单 SQL"
        assert case.count_marker in dialect.sql_count_tables(scope).sql, f"{where} 计数 SQL"

        # ---- 内省 SQL 都是 BoundSQL，且不残留 f-string 占位 ----
        for bound in _all_introspection_sql(dialect, scope):
            assert isinstance(bound, BoundSQL), f"{where} 内省 SQL 不是 BoundSQL"
            assert "{" not in bound.sql, f"{where} 内省 SQL 残留 f-string 占位"


def _all_introspection_sql(dialect: Any, scope: CatalogScope) -> list[BoundSQL]:
    """收集方言的全部内省 SQL（含可选的超时设置语句）。"""
    from dbchat.db.dialects.base import RowCountMode

    statements = [
        dialect.sql_list_tables(scope),
        dialect.sql_list_columns(scope),
        dialect.sql_list_foreign_keys(scope),
        dialect.sql_list_indexes(scope),
        dialect.sql_row_count("t"),
        dialect.sql_row_count("t", RowCountMode.APPROX),
        dialect.sql_sample("t", ["a"], 3),
        dialect.sql_checksum(scope),
        dialect.sql_count_tables(scope),
    ]
    timeout_sql = dialect.sql_set_statement_timeout(1000)
    if timeout_sql is not None:
        statements.append(timeout_sql)
    return statements


def test_identifiers_are_escaped_never_interpolated() -> None:
    """带引号的表名必须被转义 —— 拼接式实现会拼出可注入的 SQL。"""
    from dbchat.db.dialects.base import RowCountMode

    for kind in registered_kinds():
        dialect = get_dialect(kind)
        where = f"[{kind.value}]"
        hostile = 'evil"; DROP TABLE x; --'
        quoted = dialect.quote(hostile)
        quote_char = dialect.identifier_quote
        assert quoted == f"{quote_char}{hostile.replace(quote_char, quote_char * 2)}{quote_char}", (
            f"{where} 标识符转义不正确: {quoted}"
        )

        # 拼出来的 SQL 里，恶意表名只能出现在引号内部
        bound = dialect.sql_row_count(hostile, RowCountMode.EXACT)
        assert quoted in bound.sql, f"{where} COUNT SQL 未使用转义后的标识符"


def test_table_names_are_bound_not_interpolated() -> None:
    """APPROX 行数查询必须用参数绑定表名，而不是拼字符串。"""
    from dbchat.db.dialects.base import RowCountMode

    for kind in registered_kinds():
        dialect = get_dialect(kind)
        where = f"[{kind.value}]"
        bound = dialect.sql_row_count("some_table", RowCountMode.APPROX)
        assert "some_table" not in bound.sql, f"{where} 表名被拼进了 SQL"
        assert "some_table" in bound.params, f"{where} 表名未出现在绑定参数里"
        assert bound.params, where

        sample = dialect.sql_sample("some_table", ["c1", "c2"], 5)
        assert "c1" in sample.sql and "c2" in sample.sql, f"{where} 列名未被引用进 SQL"
        assert "some_table" in sample.sql, where
        assert dialect.limit_clause(5) in sample.sql, f"{where} 抽样 SQL 未限流"


@pytest.mark.parametrize(
    ("data_type", "column_type", "expected"),
    [
        ("decimal", "decimal(10,2)", TypeFamily.NUMERIC),
        ("date", "date", TypeFamily.DATETIME),
    ],
)
def test_value_coercion_is_jsonable(
    data_type: str, column_type: str, expected: TypeFamily
) -> None:
    """驱动特定类型必须被转成可 JSON 化的值（三个方言一致）。"""
    for kind in registered_kinds():
        dialect = get_dialect(kind)
        where = f"[{kind.value}]"
        assert isinstance(dialect.coerce_value(decimal.Decimal("1.5")), float), where
        assert isinstance(dialect.coerce_value(datetime.datetime(2024, 1, 2, 3, 4, 5)), str), where
        assert isinstance(dialect.coerce_value(datetime.date(2024, 1, 2)), str), where
        assert dialect.coerce_value(None) is None, where
        assert dialect.coerce_value(True) is True, where
        assert dialect.coerce_value(b"\xff\xfe") == "0xfffe", where
        assert dialect.coerce_value(b"hello") == "hello", where
        assert dialect.type_family(data_type, column_type) is expected, where


def test_default_schema_uses_profile_database() -> None:
    """连接后应切入 profile 指定的 schema。"""
    for kind in registered_kinds():
        dialect = get_dialect(kind)
        profile = DatabaseProfile(id="x", database="training_exam_db", kind=kind)
        assert dialect.default_schema(profile) == "training_exam_db", kind.value
