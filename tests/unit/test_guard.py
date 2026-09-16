"""SQL 校验 —— 针对历史缺陷的回归测试。

每一条都对应 `app/database/sql_validator.py` 里的一处漏检：

* 表名正则只认反引号 -> `FROM "T_USER"` 逃过存在性检查与表筛选检查，
  `FROM SYS.ALL_TABLES` 被解析成表 `SYS`；
* 限流用字符串拼接 -> 给达梦补 `LIMIT`（达梦不认），并且直接跳过
  `first_word != "SELECT"` 的 CTE 查询（一行都不限）；
* 注释/字符串用正则剥离 -> 与字面量互相干扰。
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlglot
from sqlglot.errors import ParseError

from dbchat.db.dialects import get_dialect
from dbchat.db.guard import extract_table_refs, inspect
from dbchat.domain.profile import DialectKind
from dbchat.domain.schema import (
    CatalogScope,
    ColumnMeta,
    ForeignKeyMeta,
    SchemaSnapshot,
    TableMeta,
)

MYSQL = get_dialect(DialectKind.MYSQL)
PG = get_dialect(DialectKind.POSTGRESQL)
DM = get_dialect(DialectKind.DAMENG)


def _table(name: str, columns: tuple[str, ...], comment: str = "") -> TableMeta:
    return TableMeta(
        name=name,
        comment=comment,
        row_count=10,
        columns=[ColumnMeta(name=c, data_type="varchar") for c in columns],
    )


@pytest.fixture
def snapshot() -> SchemaSnapshot:
    """两张表的快照：employee(id, name, dept_id) / department(id, name)。"""
    employee = _table("employee", ("id", "name", "dept_id"))
    department = _table("department", ("id", "name"))
    employee.foreign_keys.append(
        ForeignKeyMeta(table="employee", column="dept_id", ref_table="department", ref_column="id")
    )
    return SchemaSnapshot(
        db_name="training_exam_db",
        tables={"employee": employee, "department": department},
    )


# ------------------------------------------------------------ 表名提取


@pytest.mark.parametrize(
    ("dialect", "sql", "expected", "expected_system"),
    [
        # 双引号表名：老正则完全看不见
        (PG, 'SELECT * FROM "T_USER"', "T_USER", False),
        (DM, 'SELECT * FROM "T_USER"', "T_USER", False),
        # 反引号表名
        (MYSQL, "SELECT * FROM `T_USER`", "T_USER", False),
        # 裸标识符
        (MYSQL, "SELECT * FROM T_USER", "T_USER", False),
        # schema 限定：老正则把它当成表 `SYS`
        (DM, "SELECT * FROM SYS.ALL_TABLES", "ALL_TABLES", True),
        (MYSQL, "SELECT * FROM mysql.user", "user", True),
        (PG, "SELECT * FROM pg_catalog.pg_class", "pg_class", True),
        # 别名
        (MYSQL, "SELECT e.name FROM employee e", "employee", False),
        (MYSQL, "SELECT e.name FROM employee AS e", "employee", False),
        # JOIN
        (MYSQL, "SELECT * FROM employee e JOIN department d ON e.dept_id = d.id", "employee", False),
        # 三段的 catalog.schema.table
        (PG, "SELECT * FROM postgres.public.employee", "employee", False),
    ],
)
def test_table_extraction(
    dialect: Any, sql: str, expected: str, expected_system: bool
) -> None:
    refs = extract_table_refs(dialect, sql)
    assert [r.name for r in refs][0] == expected, f"{sql} -> {[r.name for r in refs]}"
    assert refs[0].is_system is expected_system, sql


def test_table_extraction_records_alias_and_schema() -> None:
    refs = extract_table_refs(MYSQL, "SELECT e.name FROM training.employee e")
    assert len(refs) == 1
    assert refs[0].name == "employee"
    assert refs[0].schema == "training"
    assert refs[0].alias == "e"
    assert refs[0].qualified == "training.employee"


def test_join_lists_every_table() -> None:
    refs = extract_table_refs(
        MYSQL, "SELECT * FROM employee e JOIN department d ON e.dept_id = d.id"
    )
    assert {r.name for r in refs} == {"employee", "department"}
    assert {r.alias for r in refs} == {"e", "d"}


def test_cte_names_are_not_treated_as_tables() -> None:
    """`WITH dept_sum AS (...) SELECT * FROM dept_sum` 里的 dept_sum 不是表。"""
    refs = extract_table_refs(
        MYSQL, "WITH dept_sum AS (SELECT dept_id FROM employee) SELECT * FROM dept_sum"
    )
    by_name = {r.name: r for r in refs}
    assert by_name["dept_sum"].is_cte is True
    assert by_name["employee"].is_cte is False


def test_cte_shadowing_does_not_raise_missing_table(snapshot: SchemaSnapshot) -> None:
    """CTE 遮蔽真实表名时不该报"表不存在"，也不该把 CTE 名报成缺表。"""
    result = inspect(
        MYSQL,
        "WITH phantom AS (SELECT id FROM employee) SELECT * FROM phantom",
        snapshot=snapshot,
        row_limit=200,
    )
    assert result.ok, result.errors

    # 反向：快照里没有的表，仍要报出来
    bad = inspect(MYSQL, "SELECT * FROM phantom", snapshot=snapshot, row_limit=None)
    assert not bad.ok
    assert "phantom" in bad.error_text()


def test_quoted_table_is_checked(snapshot: SchemaSnapshot) -> None:
    """这条正是老正则漏掉的：`FROM "T_USER"` 必须被查出表不存在。"""
    result = inspect(PG, 'SELECT * FROM "T_USER"', snapshot=snapshot, row_limit=None)
    assert not result.ok
    assert "T_USER" in result.error_text()

    result = inspect(MYSQL, "SELECT * FROM `T_USER`", snapshot=snapshot, row_limit=None)
    assert not result.ok
    assert "T_USER" in result.error_text()


def test_system_tables_are_exempt(snapshot: SchemaSnapshot) -> None:
    """系统视图/表永远允许，不参与存在性检查，也不受表筛选限制。"""
    for dialect, sql in (
        (DM, "SELECT * FROM SYS.ALL_TABLES"),
        (DM, "SELECT * FROM ALL_TAB_COLUMNS"),
        (MYSQL, "SELECT * FROM information_schema.TABLES"),
        (PG, "SELECT * FROM information_schema.columns"),
        (PG, "SELECT * FROM pg_catalog.pg_class"),
        (DM, "SELECT 1 FROM DUAL"),
    ):
        result = inspect(
            dialect,
            sql,
            snapshot=snapshot,
            scope=CatalogScope(frozenset({"employee"})),
            row_limit=None,
        )
        assert result.ok, f"{dialect.kind.value} {sql}: {result.errors}"


# ------------------------------------------------------------ 行数限制


def test_row_limit_applies_to_cte(snapshot: SchemaSnapshot) -> None:
    """CTE 查询也必须限流 —— 老实现对 `WITH` 直接跳过，整表拉进内存。"""
    result = inspect(
        MYSQL,
        "WITH x AS (SELECT id FROM employee) SELECT * FROM x",
        snapshot=snapshot,
        row_limit=200,
    )
    assert result.ok, result.errors
    assert result.limited is True
    assert result.sql.rstrip().endswith("LIMIT 200"), result.sql


def test_row_limit_never_emits_limit_for_dameng(snapshot: SchemaSnapshot) -> None:
    """达梦不认 LIMIT —— 任何路径都不能给它补出 LIMIT。"""
    for sql in (
        "SELECT * FROM EMPLOYEE",
        "SELECT * FROM EMPLOYEE WHERE ID > 1",
        "WITH x AS (SELECT ID FROM EMPLOYEE) SELECT * FROM x",
        "SELECT ID FROM EMPLOYEE ORDER BY ID",
    ):
        result = inspect(DM, sql, snapshot=snapshot, row_limit=200)
        assert result.ok, result.errors
        assert result.limited is True, sql
        assert "LIMIT" not in result.sql.upper(), result.sql
        assert result.sql.rstrip().endswith("FETCH FIRST 200 ROWS ONLY"), result.sql


def test_row_limit_skipped_when_already_limited(snapshot: SchemaSnapshot) -> None:
    for dialect, sql in (
        (MYSQL, "SELECT * FROM employee LIMIT 5"),
        (MYSQL, "SELECT * FROM employee LIMIT 5 OFFSET 10"),
        (PG, "SELECT * FROM employee LIMIT 5"),
        (DM, "SELECT * FROM EMPLOYEE WHERE ROWNUM <= 5"),
        (DM, "SELECT * FROM EMPLOYEE FETCH FIRST 5 ROWS ONLY"),
    ):
        result = inspect(dialect, sql, snapshot=snapshot, row_limit=200)
        assert result.limited is False, f"{dialect.kind.value} {sql} -> {result.sql}"
        assert result.sql == sql, result.sql


def test_row_limit_skipped_for_single_row_aggregate(snapshot: SchemaSnapshot) -> None:
    """COUNT(*) 天然一行，不必限流；但带 GROUP BY 的仍要限。"""
    plain = inspect(MYSQL, "SELECT COUNT(*) FROM employee", snapshot=snapshot, row_limit=200)
    assert plain.limited is False

    grouped = inspect(
        MYSQL, "SELECT dept_id, COUNT(*) FROM employee GROUP BY dept_id",
        snapshot=snapshot, row_limit=200,
    )
    assert grouped.limited is True
    assert grouped.sql.rstrip().endswith("LIMIT 200")


def test_row_limit_can_be_disabled(snapshot: SchemaSnapshot) -> None:
    result = inspect(MYSQL, "SELECT * FROM employee", snapshot=snapshot, row_limit=None)
    assert result.limited is False
    assert result.sql == "SELECT * FROM employee"


def test_row_limit_not_applied_to_show(snapshot: SchemaSnapshot) -> None:
    result = inspect(MYSQL, "SHOW TABLES", row_limit=200)
    assert result.limited is False


# -------------------------------------------------------- 解析失败兜底


def test_parse_error_falls_back_and_never_rejects() -> None:
    """解析失败必须退到保守路径，绝不能变成硬拒绝。"""
    broken = "SELECT * FROM employee WHERE x IN ("
    result = inspect(MYSQL, broken, row_limit=200)
    assert result.fell_back is True
    assert result.ok, "解析失败不该导致拒绝执行"
    assert result.warnings, "兜底必须有提示"
    assert "LIMIT 200" in result.sql


def test_parse_error_fallback_uses_dialect_limit_clause() -> None:
    """兜底路径同样要遵守方言的限流写法（达梦得到 FETCH FIRST）。"""
    broken = "SELECT * FROM EMPLOYEE WHERE X IN ("
    result = inspect(DM, broken, row_limit=200)
    assert result.fell_back is True
    assert "LIMIT" not in result.sql.upper()
    assert result.sql.rstrip().endswith("FETCH FIRST 200 ROWS ONLY")


def test_parse_error_fallback_does_not_invent_missing_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析失败时"表不存在"降级为不报错 —— 解析不准不能变成硬拒绝。"""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ParseError("模拟 sqlglot 不认识达梦的语法")

    monkeypatch.setattr(sqlglot, "parse_one", _boom)

    result = inspect(
        DM,
        "SELECT X FROM DUAL WHERE NVL(A, 0) = 1 AND B(+) = C",
        snapshot=_snapshot_without_dual(),
        row_limit=200,
    )
    assert result.fell_back is True
    assert result.ok, result.errors
    assert "LIMIT" not in result.sql.upper()


def test_parse_error_fallback_still_enforces_table_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底路径可以漏检存在性，但**表筛选范围**是安全控制，必须继续生效。"""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ParseError("模拟解析失败")

    monkeypatch.setattr(sqlglot, "parse_one", _boom)
    result = inspect(
        MYSQL,
        "SELECT * FROM secret_table",
        scope=CatalogScope(frozenset({"employee"})),
        row_limit=200,
    )
    assert result.fell_back is True
    assert not result.ok
    assert "secret_table" in result.error_text()


def _snapshot_without_dual() -> SchemaSnapshot:
    return SchemaSnapshot(db_name="d", tables={"EMPLOYEE": _table("EMPLOYEE", ("A", "B", "C"))})


# ---------------------------------------------------------- 只读强制


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM employee",
        "UPDATE employee SET name = 'x'",
        "INSERT INTO employee VALUES (1)",
        "DROP TABLE employee",
        "TRUNCATE TABLE employee",
        "ALTER TABLE employee ADD COLUMN x INT",
        "CREATE TABLE x (a INT)",
        "GRANT ALL ON employee TO u",
        "SELECT * FROM employee INTO OUTFILE '/tmp/x'",
        "SELECT SLEEP(10)",
        "SELECT pg_sleep(10)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT * FROM employee; DROP TABLE employee",
        "SET SESSION sql_mode = ''",
        "CALL some_proc()",
        "VACUUM employee",
    ],
)
def test_write_statements_are_rejected(sql: str) -> None:
    for dialect in (MYSQL, PG, DM):
        result = inspect(dialect, sql, row_limit=200)
        assert not result.ok, f"{dialect.kind.value} 放过了: {sql}"


def test_string_literals_are_not_mistaken_for_statements() -> None:
    """`WHERE name = 'DELETE'` 只是数据。"""
    for sql in (
        "SELECT * FROM t WHERE name = 'DELETE'",
        "SELECT * FROM t WHERE note = 'DROP TABLE x'",
        "SELECT 'INSERT' AS word",
    ):
        result = inspect(MYSQL, sql, row_limit=None)
        assert result.ok, f"被字面量误伤: {sql} -> {result.errors}"


def test_scalar_function_is_allowed_but_statement_is_not() -> None:
    """`REPLACE(...)` 是只读标量函数；`REPLACE INTO` 是写语句。"""
    assert inspect(MYSQL, "SELECT REPLACE(name, 'a', 'b') FROM employee", row_limit=None).ok
    assert not inspect(MYSQL, "REPLACE INTO employee VALUES (1)", row_limit=None).ok
    assert not inspect(
        MYSQL, "INSERT INTO t SELECT REPLACE(a, 'x', 'y') FROM employee", row_limit=None
    ).ok


@pytest.mark.parametrize(
    ("dialect", "sql", "ok"),
    [
        (MYSQL, "SHOW TABLES", True),
        (MYSQL, "DESCRIBE employee", True),
        (PG, "SHOW TABLES", False),
        (PG, "DESCRIBE employee", False),
        (DM, "SHOW TABLES", False),
        (DM, "DESCRIBE EMPLOYEE", False),
        # TABLE t / VALUES 是 MySQL/PG 语法，达梦不认
        (PG, "TABLE employee", True),
        (DM, "TABLE EMPLOYEE", False),
        (MYSQL, "VACUUM", False),
        (PG, "EXPLAIN SELECT 1", True),
    ],
)
def test_dialect_aware_command_allowlist(dialect: Any, sql: str, ok: bool) -> None:
    result = inspect(dialect, sql, row_limit=None)
    assert result.ok is ok, f"{dialect.kind.value} {sql}: {result.errors}"


def test_empty_sql_is_rejected() -> None:
    for sql in ("", "   ", "-- 只有注释"):
        assert not inspect(MYSQL, sql, row_limit=None).ok, sql


# --------------------------------------------------------- 表筛选范围


def test_scope_blocks_tables_outside_filter(snapshot: SchemaSnapshot) -> None:
    scope = CatalogScope(frozenset({"employee"}))
    allowed = inspect(MYSQL, "SELECT id FROM employee", snapshot=snapshot, scope=scope, row_limit=None)
    assert allowed.ok, allowed.errors

    blocked = inspect(
        MYSQL, "SELECT id FROM department", snapshot=snapshot, scope=scope, row_limit=None
    )
    assert not blocked.ok
    assert "不在允许的表筛选列表中" in blocked.error_text()


def test_scope_allows_everything_when_unset(snapshot: SchemaSnapshot) -> None:
    result = inspect(
        MYSQL,
        "SELECT id FROM department",
        snapshot=snapshot,
        scope=CatalogScope(),
        row_limit=None,
    )
    assert result.ok, result.errors


# ------------------------------------------------------------ 列校验


def test_missing_qualified_column_is_an_error(snapshot: SchemaSnapshot) -> None:
    result = inspect(MYSQL, "SELECT employee.nope FROM employee", snapshot=snapshot, row_limit=None)
    assert not result.ok
    assert "nope" in result.error_text()

    ok = inspect(MYSQL, "SELECT employee.name FROM employee", snapshot=snapshot, row_limit=None)
    assert ok.ok, ok.errors


def test_missing_unqualified_column_is_only_a_warning(snapshot: SchemaSnapshot) -> None:
    result = inspect(MYSQL, "SELECT nope FROM employee", snapshot=snapshot, row_limit=None)
    assert result.ok, result.errors
    assert any("nope" in w for w in result.warnings), result.warnings


def test_aliases_and_aggregates_are_not_columns(snapshot: SchemaSnapshot) -> None:
    for sql in (
        "SELECT COUNT(*) AS total FROM employee ORDER BY total",
        "SELECT e.name AS who FROM employee e ORDER BY who",
        "SELECT e.name FROM employee e WHERE e.dept_id = 1",
        "SELECT d.name, e.name FROM employee e JOIN department d ON e.dept_id = d.id",
    ):
        result = inspect(MYSQL, sql, snapshot=snapshot, row_limit=None)
        assert result.ok, f"{sql} -> {result.errors}"
        assert not [w for w in result.warnings if "未找到" in w], f"{sql} -> {result.warnings}"


def test_select_star_warns(snapshot: SchemaSnapshot) -> None:
    result = inspect(MYSQL, "SELECT * FROM employee", snapshot=snapshot, row_limit=None)
    assert any("SELECT *" in w for w in result.warnings)


def test_validation_without_snapshot_skips_structure_checks() -> None:
    """没有快照时只做只读与限流，不妄断表列是否存在。"""
    result = inspect(MYSQL, "SELECT * FROM whatever", snapshot=None, row_limit=200)
    assert result.ok, result.errors
    assert result.limited is True


def test_validation_never_mutates_callers_sql(snapshot: SchemaSnapshot) -> None:
    """校验只返回新 SQL，不改调用方手里的字符串。"""
    sql = "SELECT * FROM employee"
    original = sql
    inspect(MYSQL, sql, snapshot=snapshot, row_limit=200)
    assert sql == original


def test_python_dict_style_placeholders_are_untouched(snapshot: SchemaSnapshot) -> None:
    """带 `%s` 字面量的用户 SQL 不会被校验流程改坏。"""
    sql = "SELECT * FROM employee WHERE name LIKE '%s%' AND id = 1"
    result = inspect(MYSQL, sql, snapshot=snapshot, row_limit=200)
    assert "LIKE '%s%'" in result.sql, result.sql
    assert "LIMIT 200" in result.sql
