"""注册表约束 —— "新增一个国产库只花一个文件"必须可验证。

这些断言把"接入成本"钉死在测试里：任何让新增方言需要改第二个文件
（多加一个抽象方法、多一个必填属性）的改动都会在这里失败。
"""

from __future__ import annotations

import inspect

import pytest

from dbchat.db.dialects import get_dialect, register_dialect, registered_kinds
from dbchat.db.dialects.base import Dialect, RowCountMode, RowLimitStyle
from dbchat.domain.errors import DbError, DbErrorKind
from dbchat.domain.profile import DialectKind
from dbchat.domain.query import BoundSQL, TypeFamily
from dbchat.domain.schema import CatalogScope


def test_all_registered_dialects_are_complete() -> None:
    """每个已注册方言都实现了全部抽象方法，且提示词非空。"""
    abstract = {
        name
        for name, member in inspect.getmembers(Dialect, predicate=inspect.isfunction)
        if getattr(member, "__isabstractmethod__", False)
    }
    assert abstract, "Dialect 竟然没有抽象方法 —— 接口被改坏了"

    for kind in registered_kinds():
        dialect = get_dialect(kind)
        where = f"[{kind.value}]"

        # 抽象方法都真的实现了（不是继承来的 abstractmethod）
        for name in abstract:
            impl = getattr(type(dialect), name, None)
            assert impl is not None, f"{where} 缺少方法 {name}"
            assert not getattr(impl, "__isabstractmethod__", False), f"{where}.{name} 仍是抽象的"

        # 必填的类属性
        assert isinstance(dialect.kind, DialectKind), where
        assert dialect.display_name.strip(), f"{where} display_name 为空"
        assert dialect.sqlglot_name.strip(), f"{where} sqlglot_name 为空"
        assert dialect.driver_package.strip(), f"{where} driver_package 为空"
        assert dialect.identifier_quote in ("`", '"'), f"{where} identifier_quote 取值异常"
        assert isinstance(dialect.row_limit_style, RowLimitStyle), where
        assert dialect.capabilities is not None, where
        assert isinstance(dialect.system_schemas, frozenset), where
        assert isinstance(dialect.disallowed_leading_keywords, frozenset), where

        # 提示词：非空、不含空串
        notes = dialect.prompt_notes()
        assert isinstance(notes, tuple) and notes, f"{where} prompt_notes 必须是非空 tuple"
        assert all(isinstance(n, str) and n.strip() for n in notes), f"{where} prompt_notes 有空串"

        # 内省 SQL 都能构造出来，且全部是 BoundSQL
        scope = CatalogScope()
        statements = {
            "sql_list_tables": dialect.sql_list_tables(scope),
            "sql_list_columns": dialect.sql_list_columns(scope),
            "sql_list_foreign_keys": dialect.sql_list_foreign_keys(scope),
            "sql_list_indexes": dialect.sql_list_indexes(scope),
            "sql_row_count": dialect.sql_row_count("t"),
            "sql_sample": dialect.sql_sample("t", ["c"], 1),
            "sql_checksum": dialect.sql_checksum(scope),
        }
        for name, bound in statements.items():
            assert bound.sql.strip(), f"{where}.{name} 返回空 SQL"
            assert isinstance(bound.params, tuple), f"{where}.{name} params 必须是 tuple"


def test_get_dialect_is_cached_singleton() -> None:
    """方言对象无状态，应当是进程内单例。"""
    for kind in registered_kinds():
        assert get_dialect(kind) is get_dialect(kind), kind.value


def test_unimplemented_kind_gives_actionable_error() -> None:
    """预留但未实现的国产库要报出"怎么接入"，不能静默退回 MySQL。"""
    with pytest.raises(DbError) as excinfo:
        get_dialect(DialectKind.KINGBASE)
    assert excinfo.value.kind is DbErrorKind.UNSUPPORTED
    assert "kingbase" in excinfo.value.user_message.lower()
    assert "register_dialect" in excinfo.value.user_message


def test_register_requires_kind() -> None:
    """没有声明 kind 的方言类必须立刻报错，而不是注册成一个隐形方言。"""

    class _Nameless(Dialect):  # type: ignore[misc]
        pass

    with pytest.raises(TypeError):
        register_dialect(_Nameless)


def test_adding_a_dialect_costs_one_file() -> None:
    """注册一个新方言只需一个类 + 一个装饰器，不改任何既有文件。"""

    @register_dialect
    class _FakeDialect(Dialect):
        kind = DialectKind.KINGBASE
        display_name = "人大金仓(KingbaseES)"
        sqlglot_name = "postgres"
        driver_package = "ksycopg2"
        identifier_quote = '"'

        def import_driver(self):
            raise DbError(DbErrorKind.UNSUPPORTED, user_message="测试用方言，不连接")

        def connect(self, profile, *, timeout_s: int = 30):
            raise DbError(DbErrorKind.UNSUPPORTED, user_message="测试用方言，不连接")

        def ping(self, conn) -> bool:
            return False

        def create_cursor(self, conn):
            raise NotImplementedError

        def classify_error(self, exc) -> DbErrorKind:
            return DbErrorKind.UNKNOWN

        def placeholder(self) -> str:
            return "%s"

        def bind(self, sql: str, count: int) -> str:
            return sql

        def normalize_row(self, row, description):
            return row or {}

        def column_names(self, description):
            return [str(c[0]) for c in (description or ())]

        def coerce_value(self, value):
            return value

        def type_family(self, data_type: str, column_type: str = "") -> TypeFamily:
            return TypeFamily.OTHER

        def sql_list_tables(self, scope):
            return BoundSQL("SELECT 1 AS table_name")

        def sql_list_columns(self, scope):
            return BoundSQL("SELECT 1 AS table_name")

        def sql_list_foreign_keys(self, scope):
            return BoundSQL("SELECT 1 AS constraint_name")

        def sql_list_indexes(self, scope):
            return BoundSQL("SELECT 1 AS table_name")

        def sql_row_count(self, table: str, mode=RowCountMode.EXACT):
            return BoundSQL("SELECT 1 AS row_count")

        def sql_sample(self, table: str, columns, n: int):
            return BoundSQL("SELECT 1")

        def sql_checksum(self, scope):
            return BoundSQL("SELECT 1 AS object_name")

        def prompt_notes(self):
            return ("这是测试方言。",)

    try:
        dialect = get_dialect(DialectKind.KINGBASE)
        assert isinstance(dialect, _FakeDialect)
        assert dialect.quote("x") == '"x"'
        assert dialect.limit_clause(3) == "LIMIT 3"
        assert dialect.prompt_notes() == ("这是测试方言。",)
        # 默认实现已经够用：系统表谓词、超时、行数上限
        assert dialect.system_table_predicate() == "1=1"
        assert dialect.sql_set_statement_timeout(1000) is None
        assert dialect.apply_row_limit("SELECT 1", 10).endswith("LIMIT 10")
        assert DialectKind.KINGBASE in registered_kinds()
    finally:
        # 只清理本测试注册的方言，别动内置的三个。
        from dbchat.db.dialects import _PENDING, _REGISTRY

        _PENDING.pop(DialectKind.KINGBASE, None)
        _REGISTRY.pop(DialectKind.KINGBASE, None)
