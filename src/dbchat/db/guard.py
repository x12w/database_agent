"""SQL 校验 —— 只读、限流、表/列存在性。

取代 `app/database/sql_validator.py`。历史实现靠正则做三件事，三件都漏：

1. **表名提取**用 `r'(?:FROM|JOIN)\\s+`?(\\w+)`?'`，只认反引号。于是
   `FROM "T_USER"`（PG / 达梦的写法）既躲过存在性检查也躲过表筛选检查，
   `FROM SYS.ALL_TABLES` 被解析成表 `SYS`。
2. **限流**用字符串拼接，给达梦也补 `LIMIT 200`（达梦不认），
   而且 `first_word != "SELECT"` 就直接返回 —— `WITH ... SELECT` 的
   CTE 查询一行都不限，全表拉进内存。
3. **注释剥离**用正则，`-- ` 与 `#` 的处理和字符串字面量互相干扰。

SQL 无法可靠解析时拒绝执行，不能用正则降级绕过只读或表范围控制。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from dbchat.db.dialects.base import Dialect
from dbchat.db.sqltext import first_keyword, mask_literals, split_statements, strip_comments
from dbchat.domain.profile import DialectKind
from dbchat.domain.schema import CatalogScope, SchemaSnapshot

__all__ = [
    "FORBIDDEN_PATTERNS",
    "SYSTEM_RELATION_NAMES",
    "GuardResult",
    "TableRef",
    "extract_table_refs",
    "inspect",
]

logger = logging.getLogger(__name__)

#: 写操作 / 危险函数。取自 `app/database/sql_validator.py:14-32`。
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    r"\bINSERT\b",
    r"\bUPDATE\b",
    r"\bDELETE\b",
    r"\bDROP\b",
    r"\bCREATE\b",
    r"\bALTER\b",
    r"\bTRUNCATE\b",
    r"\bRENAME\b",
    r"\bGRANT\b",
    r"\bREVOKE\b",
    r"\bREPLACE\b",
    r"\bLOAD\b",
    r"\bCALL\b",
    r"\bEXEC\b",
    r"\bEXECUTE\b",
    r"\bPREPARE\b",
    r"\bFLUSH\b",
    r"\bSET\b",
    r"\bLOCK\b",
    r"\bUNLOCK\b",
    r"\bKILL\b",
    r"\bSHUTDOWN\b",
    r"\bHANDLER\b",
    r"\bINTO\s+(OUTFILE|DUMPFILE)\b",
    r"\bSLEEP\s*\(.*?\)",
    r"\bBENCHMARK\s*\(.*?\)",
    r"\bWAITFOR\s+DELAY\b",
    r"\bVACUUM\b",
    r"\bREINDEX\b",
    r"\bDISCARD\b",
    r"\bCOPY\b",
    r"\bNOTIFY\b",
    r"\bLISTEN\b",
    r"\bREFRESH\s+MATERIALIZED\b",
    r"\bpg_sleep\s*\(.*?\)",
    r"\bpg_read_file\b",
    r"\bpg_write_file\b",
    r"\bpg_read_binary_file\b",
    r"\blo_import\b",
    r"\blo_export\b",
)

_FORBIDDEN_COMPILED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in FORBIDDEN_PATTERNS
)

#: 例外：这些词紧跟 `(` 时是只读的标量函数，不是语句。
#: `SELECT REPLACE(name,'a','b')` 合法；危险的 `REPLACE INTO t` 后面
#: 跟的不是括号，照样会被拦下（`INSERT ... SELECT REPLACE(...)` 也拦得住，
#: 因为 INSERT 本身命中黑名单）。
_SCALAR_FUNCTION_WORDS = frozenset({"REPLACE"})

#: 系统关系名 —— 未加限定名时用来判断"这是系统对象，不用校验"。
#: （限定名走 `Dialect.system_schemas`。）
SYSTEM_RELATION_NAMES: frozenset[str] = frozenset(
    {
        # Oracle / 达梦
        "all_tables",
        "all_tab_columns",
        "all_tab_comments",
        "all_col_comments",
        "all_constraints",
        "all_cons_columns",
        "all_indexes",
        "all_ind_columns",
        "all_views",
        "all_users",
        "all_objects",
        "all_sequences",
        "all_tab_partitions",
        "user_tables",
        "user_tab_columns",
        "user_constraints",
        "user_cons_columns",
        "user_indexes",
        "user_ind_columns",
        "user_views",
        "user_users",
        "user_objects",
        "dba_tables",
        "dba_tab_columns",
        "dba_constraints",
        "dual",
        # MySQL
        "information_schema",
        "mysql",
        "performance_schema",
        "sys",
        # PostgreSQL
        "pg_catalog",
        "pg_tables",
        "pg_indexes",
        "pg_class",
        "pg_namespace",
        "pg_attribute",
        "pg_constraint",
        "pg_stat_user_tables",
        "pg_views",
        "pg_proc",
        "pg_type",
        "pg_description",
        "pg_settings",
        # SQLite（历史遗留，保留以免破坏性变更）
        "sqlite_master",
        "sqlite_sequence",
    }
)

_SELECT_STAR_PATTERN = re.compile(r"\bSELECT\s+\*", re.IGNORECASE)

#: 加了限流之后仍要保留的可比对面（用于确认 AST 重写没改坏查询）。
_STRUCTURE_SQL_ARGS = ("from_", "joins", "where", "group", "having", "order", "qualify")


@dataclass(slots=True, frozen=True)
class TableRef:
    """SQL 里引用到的一张表。"""

    name: str
    schema: str | None = None
    alias: str | None = None
    catalog: str | None = None
    #: 指向 CTE 定义而非真实表。
    is_cte: bool = False
    #: 系统对象（系统 schema 限定，或命中系统关系名）。
    is_system: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass(slots=True)
class GuardResult:
    """校验结果。"""

    sql: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables: list[TableRef] = field(default_factory=list)
    #: 是否自动补了行数上限。
    limited: bool = False
    #: 是否出现解析失败（兼容诊断字段；现在一律拒绝执行）。
    fell_back: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def error_text(self) -> str:
        return "; ".join(self.errors)


# ------------------------------------------------------------------ 校验


def inspect(
    dialect: Dialect,
    sql: str,
    *,
    snapshot: SchemaSnapshot | None = None,
    scope: CatalogScope | None = None,
    row_limit: int | None = None,
    allowed_schemas: frozenset[str] | None = None,
) -> GuardResult:
    """校验一条待执行的 SQL，并返回可能被改写过的语句。

    Args:
        dialect: 目标方言（决定占位符无关的部分：首关键字白名单、
            sqlglot 方言、限流写法）。
        sql: 待校验的 SQL。
        snapshot: 结构快照；给出时才做表/列存在性校验。
        scope: 表筛选范围；给出时强制"只能查这些表"。
        row_limit: 行数上限；None 表示不限流。

    Returns:
        `GuardResult`。`errors` 非空时调用方**不得**执行 `sql`。
    """
    result = GuardResult(sql=sql)
    if not sql or not sql.strip():
        result.errors.append("SQL 为空")
        return result

    statements = split_statements(sql)
    if len(statements) > 1:
        result.errors.append("不允许一次执行多条语句")
        return result
    body = statements[0] if statements else sql

    # 注释剥离后再看首关键字：`/*c*/ SELECT` 的首关键字必须是 SELECT。
    cleaned = strip_comments(body).strip().rstrip(";").strip()
    if not cleaned:
        result.errors.append("SQL 去掉注释后为空")
        return result

    if first_keyword(cleaned) not in dialect.allowed_leading_keywords:
        _check_read_only(dialect, cleaned, result)
        return result

    # Normalize shorthand before AST validation. EXPLAIN ANALYZE is deliberately
    # rejected because it actually executes its target.
    if first_keyword(cleaned) == "TABLE":
        cleaned = "SELECT * FROM " + cleaned[5:].strip()
    if first_keyword(cleaned) == "EXPLAIN":
        inner = cleaned[7:].strip()
        if first_keyword(inner) not in {"SELECT", "WITH"}:
            result.errors.append("只支持 EXPLAIN SELECT/WITH（不允许 ANALYZE）")
            return result
        checked = inspect(
            dialect,
            inner,
            snapshot=snapshot,
            scope=scope,
            row_limit=row_limit,
            allowed_schemas=allowed_schemas,
        )
        checked.sql = "EXPLAIN " + checked.sql
        return checked

    _check_read_only(dialect, cleaned, result)
    if result.errors:
        return result

    tree = _parse(dialect, cleaned)
    if tree is None:
        result.fell_back = True
        result.errors.append("SQL 无法可靠解析，请改写为受支持的只读查询")
        return result
    _check_ast_read_only(tree, result)
    if result.errors:
        return result

    try:
        limited_sql, limited, fell_back = _apply_row_limit(dialect, cleaned, row_limit)
        refs = extract_table_refs(dialect, limited_sql)
    except (ValueError, sqlglot.errors.SqlglotError) as exc:
        result.errors.append(f"无法安全分析或限制查询: {exc}")
        return result
    result.sql = limited_sql
    result.limited = limited
    result.fell_back = fell_back
    result.tables = refs
    schemas = (
        allowed_schemas
        if allowed_schemas is not None
        else frozenset({snapshot.db_name} if snapshot else ())
    )
    for ref in refs:
        if ref.is_cte:
            continue
        if ref.catalog or (
            ref.schema
            and not ref.is_system
            and ref.schema.lower() not in {s.lower() for s in schemas}
        ):
            result.errors.append(f"不允许跨数据库或未授权 schema 访问: {ref.qualified}")
        if (
            ref.is_system
            and ref.schema
            and (
                ref.schema.lower() not in {"information_schema", "pg_catalog", "sys"}
                or (
                    ref.schema.lower() != "information_schema"
                    and ref.name.lower() not in SYSTEM_RELATION_NAMES
                )
            )
        ):
            result.errors.append(f"不允许访问系统数据表: {ref.qualified}")

    _check_tables(refs, snapshot, scope, result)
    _check_columns(result.sql, dialect, refs, snapshot, result)

    if _SELECT_STAR_PATTERN.search(mask_literals(result.sql)):
        result.warnings.append("使用了 SELECT *，建议只选择需要的列以提高效率")

    return result


# Unknown/UDF functions are not a read-only boundary. Allow only known AST
# builtins plus a small set of documented scalar/metadata functions.
_SAFE_ANONYMOUS = frozenset(
    {
        "DATABASE",
        "SCHEMA",
        "CURRENT_SCHEMA",
        "CURRENT_DATABASE",
        "VERSION",
        "TO_CHAR",
        "TO_DATE",
        "TO_TIMESTAMP",
        "NVL",
        "DECODE",
        "FORMAT",
        "PG_GET_EXPR",
        "PG_GET_CONSTRAINTDEF",
        "PG_GET_INDEXDEF",
        "PG_TABLE_IS_VISIBLE",
        "OBJ_DESCRIPTION",
        "COL_DESCRIPTION",
    }
)
_UNSAFE_FUNCTIONS = frozenset(
    {
        "LOAD_FILE",
        "SETVAL",
        "NEXTVAL",
        "NEXT_VALUE_FOR",
        "SLEEP",
        "PG_SLEEP",
        "BENCHMARK",
        "PG_READ_FILE",
        "PG_READ_BINARY_FILE",
        "LO_IMPORT",
        "LO_EXPORT",
    }
)


def _check_ast_read_only(tree: exp.Expression, result: GuardResult) -> None:
    if not isinstance(tree, (exp.Query, exp.Show, exp.Describe)):
        result.errors.append("只允许可解析的只读查询")
        return
    for node in tree.walk():
        if isinstance(node, (exp.DDL, exp.DML, exp.Into, exp.Lock, exp.Command)):
            result.errors.append("SQL 包含写入或锁定操作")
            return
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
            if (
                name in _UNSAFE_FUNCTIONS
                or isinstance(node.parent, exp.Dot)
                or (isinstance(node, exp.Anonymous) and name not in _SAFE_ANONYMOUS)
            ):
                result.errors.append(f"不允许未确认只读的函数: {name}")
                return


def _check_read_only(dialect: Dialect, cleaned: str, result: GuardResult) -> None:
    """首关键字白名单 + 危险模式黑名单。"""
    keyword = first_keyword(cleaned)
    if keyword and keyword in dialect.disallowed_leading_keywords:
        result.errors.append(
            f"'{keyword}' 命令在当前数据库（{dialect.display_name}）不支持，请改用 SELECT 查询系统表"
        )
        return
    if keyword and keyword not in dialect.allowed_leading_keywords:
        allowed = ", ".join(sorted(dialect.allowed_leading_keywords))
        result.errors.append(f"不支持的语句类型 '{keyword}'，只允许: {allowed}")
        return

    # 黑名单跑在"抹掉字符串字面量"的文本上：
    # `WHERE name = 'DELETE'` 是数据，不是写操作。
    #
    # 必须大小写不敏感地匹配：历史实现把 SQL 转成大写后拿小写模式去搜，
    # 于是所有 `pg_sleep` / `pg_read_file` 这类小写模式永远匹配不到，
    # 等于形同虚设。
    haystack = mask_literals(cleaned)
    for pattern in _FORBIDDEN_COMPILED:
        match = pattern.search(haystack)
        if match is None:
            continue
        word = match.group(0).strip()
        if word.upper() in _SCALAR_FUNCTION_WORDS and haystack[match.end() :].lstrip().startswith(
            "("
        ):
            continue
        result.errors.append(f"语句包含被禁止的模式: {word}")
        return


def _check_tables(
    refs: list[TableRef],
    snapshot: SchemaSnapshot | None,
    scope: CatalogScope | None,
    result: GuardResult,
) -> None:
    """表存在性 + 表筛选范围。"""
    for ref in refs:
        if ref.is_cte or ref.is_system:
            continue
        if scope is not None and scope.tables is not None and not scope.allows(ref.name):
            result.errors.append(
                f"表 '{ref.name}' 不在允许的表筛选列表中。允许的表: "
                f"{', '.join(sorted(scope.tables))}"
            )
            continue
        if snapshot is None or not snapshot.tables:
            continue
        if snapshot.resolve_table_name(ref.name) is None:
            available = sorted(snapshot.tables)[:10]
            suffix = "..." if snapshot.table_count > 10 else ""
            result.errors.append(f"表 '{ref.name}' 不存在。可用表: {', '.join(available)}{suffix}")


def _check_columns(
    sql: str,
    dialect: Dialect,
    refs: list[TableRef],
    snapshot: SchemaSnapshot | None,
    result: GuardResult,
) -> None:
    """列存在性：限定列名对不上是错误，未限定列名找不到只给提示。"""
    if snapshot is None or not snapshot.tables:
        return
    tree = _parse(dialect, sql)
    if tree is None:
        return

    real_tables = [r for r in refs if not r.is_cte and not r.is_system]
    if not real_tables:
        return

    alias_map: dict[str, TableRef] = {}
    for ref in real_tables:
        alias_map[ref.name.lower()] = ref
        if ref.alias:
            alias_map[ref.alias.lower()] = ref

    known = _known_column_names(snapshot, real_tables)
    select_aliases = _select_aliases(tree)
    cte_names = {r.name.lower() for r in refs if r.is_cte}

    reported: set[str] = set()
    for column in tree.find_all(exp.Column):
        name = column.name
        if not name:
            continue
        lowered = name.lower()
        if lowered in select_aliases or lowered in cte_names:
            continue

        qualifier = column.table
        if qualifier:
            ref = alias_map.get(qualifier.lower())
            if ref is None:
                continue  # 别名指向子查询/未识别的表，不妄下结论
            meta = snapshot.get_table(ref.name)
            if meta is None or not meta.columns:
                continue
            if meta.column(lowered) is None:
                key = f"{qualifier}.{name}"
                if key in reported:
                    continue
                reported.add(key)
                result.errors.append(
                    f"列 '{qualifier}.{name}' 不存在于表 '{meta.name}'。可用列: "
                    f"{', '.join(meta.column_names()[:8])}"
                )
            continue

        if lowered not in known and not _ALLOWED_UNQUALIFIED.match(name):
            key = f"?.{name}"
            if key in reported:
                continue
            reported.add(key)
            result.warnings.append(f"列 '{name}' 在引用表中未找到，可能是函数、别名或常量")


#: 未限定但合法出现的名字（常量、伪列、常见日期部件）。
_ALLOWED_UNQUALIFIED = re.compile(
    r"^(true|false|null|dual|rownum|rowid|sysdate|current_date|current_time|"
    r"current_timestamp|localtime|localtimestamp|year|month|day|hour|minute|"
    r"second|value)$",
    re.IGNORECASE,
)


def _known_column_names(snapshot: SchemaSnapshot, refs: list[TableRef]) -> set[str]:
    """所有被引用表里出现过的列名（小写）。"""
    known: set[str] = set()
    for ref in refs:
        meta = snapshot.get_table(ref.name)
        if meta is None:
            continue
        known.update(c.name.lower() for c in meta.columns)
    return known


def _select_aliases(tree: exp.Expression) -> set[str]:
    """SELECT 列表里定义的别名与 CTE 列名 —— 它们不是物理列。"""
    aliases: set[str] = set()
    for node in tree.find_all(exp.Alias):
        if node.alias:
            aliases.add(node.alias.lower())
    for cte in tree.find_all(exp.CTE):
        for column in cte.alias_column_names:
            aliases.add(column.lower())
    return aliases


# ------------------------------------------------------------------ 表提取


def extract_table_refs(dialect: Dialect, sql: str) -> list[TableRef]:
    """取出 SQL 引用到的所有表。

    使用 AST 与 SQL 作用域区分真实表和 CTE；无法解析时拒绝继续。
    """
    tree = _parse(dialect, sql)
    if tree is None:
        raise ValueError("无法解析表引用")

    cte_nodes: set[int] = set()
    for query_scope in traverse_scope(tree):
        for node, source in query_scope.selected_sources.values():
            if isinstance(source, Scope) and isinstance(node, exp.Table):
                cte_nodes.add(id(node))
    refs: list[TableRef] = []
    for node in tree.find_all(exp.Table):
        if not node.name:
            continue
        refs.append(
            TableRef(
                name=node.name,
                schema=node.db or None,
                catalog=node.catalog or None,
                alias=node.alias or None,
                is_cte=id(node) in cte_nodes,
                is_system=_is_system(dialect, node.name, node.db or None),
            )
        )
    return refs


def _is_system(dialect: Dialect, name: str, schema: str | None) -> bool:
    """系统对象判定：schema 限定走方言，未限定走共享名单。"""
    if schema:
        return schema.lower() in {s.lower() for s in dialect.system_schemas}
    name = name.lower()
    if name not in SYSTEM_RELATION_NAMES:
        return False
    if dialect.kind == DialectKind.POSTGRESQL:
        return name.startswith("pg_") and name != "pg_catalog"
    if dialect.kind == DialectKind.DAMENG:
        return name == "dual" or name.startswith(("all_", "user_", "dba_"))
    return name == "dual"


# ------------------------------------------------------------------ 限流


def _apply_row_limit(dialect: Dialect, sql: str, row_limit: int | None) -> tuple[str, bool, bool]:
    """补行数上限。

    Returns:
        (改写后的 SQL, 是否补了, 是否走了兜底路径)
    """
    if row_limit is None or row_limit <= 0:
        return sql, False, False

    tree = _parse(dialect, sql)
    if tree is None:
        raise ValueError("无法解析行数限制")

    if not _needs_limit(tree, row_limit):
        return sql, False, False

    try:
        rewritten = tree.limit(row_limit).sql(dialect=dialect.sqlglot_name)
    except Exception as exc:
        raise ValueError("无法生成有界查询") from exc
    if not _same_query(tree, dialect, rewritten):
        raise ValueError("补充行数上限改变了查询结构")

    return rewritten, True, False


def _parse(dialect: Dialect, sql: str) -> exp.Expression | None:
    """解析 SQL；失败返回 None（并且只记 warning，不抛）。"""
    try:
        statements = sqlglot.parse(sql, read=dialect.sqlglot_name)
        if len(statements) != 1:
            return None
        return sqlglot.parse_one(sql, read=dialect.sqlglot_name)
    except ParseError as exc:
        logger.warning("sqlglot 解析失败 (%s): %s", dialect.sqlglot_name, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 解析器异常也必须拒绝执行
        logger.warning(
            "sqlglot 解析异常 (%s): %s: %s", dialect.sqlglot_name, type(exc).__name__, exc
        )
        return None


def _needs_limit(tree: exp.Expression, row_limit: int) -> bool:
    if not isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)):
        return False
    limit = tree.args.get("limit")
    if limit is not None:
        count = limit.args.get("expression") or limit.args.get("count")
        options = limit.args.get("limit_options")
        plain = options is None or not (
            options.args.get("percent") or options.args.get("with_ties")
        )
        return not (
            plain
            and isinstance(count, exp.Literal)
            and count.is_int
            and 0 <= int(count.this) <= row_limit
        )
    # A column named ROWNUM is not a portable bound; always cap the outer query.
    # Only a top-level aggregate without grouping/windows is guaranteed to be one row.
    if isinstance(tree, exp.Select) and not tree.args.get("group") and not tree.find(exp.Window):
        expressions = [e.unalias() for e in tree.expressions]
        if (
            expressions
            and all(isinstance(e, (exp.AggFunc, exp.Literal)) for e in expressions)
            and any(isinstance(e, exp.AggFunc) for e in expressions)
        ):
            return False
    return True


def _same_query(before: exp.Expression, dialect: Dialect, rewritten: str) -> bool:
    """确认重写后的 SQL 与原查询是同一个查询（只多了限流）。"""
    after = _parse(dialect, rewritten)
    if after is None:
        return False
    return _fingerprint(before) == _fingerprint(after)


def _clause_text(value: Any) -> str:
    """把 sqlglot 的参数值规范成文本。

    **参数值可能是单个表达式，也可能是表达式列表。** 例如 `Select.args["joins"]`
    是 `list[exp.Join]`，`args["from"]` 却是单个 `From`。早先直接对参数值调
    `.sql()`，于是**任何带 JOIN 且需要补行数限制的查询都会在这里崩**
    （`AttributeError: 'list' object has no attribute 'sql'`），
    工具调用失败、用户拿到报错。
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(_clause_text(item) for item in value)
    sql = getattr(value, "sql", None)
    return sql() if callable(sql) else str(value)


def _fingerprint(tree: exp.Expression) -> tuple[Any, ...]:
    """结构指纹：表集合 + 列集合 + 各子句的规范化文本。"""
    tables = sorted(node.sql() for node in tree.find_all(exp.Table))
    columns = sorted(node.sql() for node in tree.find_all(exp.Column))
    clauses = tuple(_clause_text(tree.args.get(key)) for key in _STRUCTURE_SQL_ARGS)
    return (tuple(tables), tuple(columns), clauses)
