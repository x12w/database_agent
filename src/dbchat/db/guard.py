"""SQL 校验 —— 只读、限流、表/列存在性。

取代 `app/database/sql_validator.py`。历史实现靠正则做三件事，三件都漏：

1. **表名提取**用 `r'(?:FROM|JOIN)\\s+`?(\\w+)`?'`，只认反引号。于是
   `FROM "T_USER"`（PG / 达梦的写法）既躲过存在性检查也躲过表筛选检查，
   `FROM SYS.ALL_TABLES` 被解析成表 `SYS`。
2. **限流**用字符串拼接，给达梦也补 `LIMIT 200`（达梦不认），
   而且 `first_word != "SELECT"` 就直接返回 —— `WITH ... SELECT` 的
   CTE 查询一行都不限，全表拉进内存。
3. **注释剥离**用正则，`-- ` 与 `#` 的处理和字符串字面量互相干扰。

这里用 sqlglot 解析成 AST 后处理（`find_all(exp.Table)` 取表、
`tree.limit(n)` 加限流），并且带一条**强制安全阀**：

    解析失败（`sqlglot.errors.ParseError`）绝不能变成"拒绝执行"。
    达梦/Oracle 的 `ROWNUM`、`NVL`、`(+)` 外连接可能过不了 sqlglot，
    这时退到保守的正则路径并记 warning —— 宁可漏检，不可误杀。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from dbchat.db.dialects.base import Dialect
from dbchat.db.sqltext import first_keyword, mask_literals, split_statements, strip_comments
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
    r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b",
    r"\bCREATE\b", r"\bALTER\b", r"\bTRUNCATE\b", r"\bRENAME\b",
    r"\bGRANT\b", r"\bREVOKE\b", r"\bREPLACE\b", r"\bLOAD\b",
    r"\bCALL\b", r"\bEXEC\b", r"\bEXECUTE\b", r"\bPREPARE\b",
    r"\bFLUSH\b", r"\bSET\b", r"\bLOCK\b", r"\bUNLOCK\b",
    r"\bKILL\b", r"\bSHUTDOWN\b", r"\bHANDLER\b",
    r"\bINTO\s+(OUTFILE|DUMPFILE)\b",
    r"\bSLEEP\s*\(.*?\)", r"\bBENCHMARK\s*\(.*?\)",
    r"\bWAITFOR\s+DELAY\b",
    r"\bVACUUM\b", r"\bREINDEX\b", r"\bDISCARD\b",
    r"\bCOPY\b", r"\bNOTIFY\b", r"\bLISTEN\b",
    r"\bREFRESH\s+MATERIALIZED\b",
    r"\bpg_sleep\s*\(.*?\)",
    r"\bpg_read_file\b", r"\bpg_write_file\b", r"\bpg_read_binary_file\b",
    r"\blo_import\b", r"\blo_export\b",
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
        "all_tables", "all_tab_columns", "all_tab_comments", "all_col_comments",
        "all_constraints", "all_cons_columns", "all_indexes", "all_ind_columns",
        "all_views", "all_users", "all_objects", "all_sequences", "all_tab_partitions",
        "user_tables", "user_tab_columns", "user_constraints", "user_cons_columns",
        "user_indexes", "user_ind_columns", "user_views", "user_users", "user_objects",
        "dba_tables", "dba_tab_columns", "dba_constraints",
        "dual",
        # MySQL
        "information_schema", "mysql", "performance_schema", "sys",
        # PostgreSQL
        "pg_catalog", "pg_tables", "pg_indexes", "pg_class", "pg_namespace",
        "pg_attribute", "pg_constraint", "pg_stat_user_tables", "pg_views",
        "pg_proc", "pg_type", "pg_description", "pg_settings",
        # SQLite（历史遗留，保留以免破坏性变更）
        "sqlite_master", "sqlite_sequence",
    }
)

#: 已经带限流的写法 —— 文本兜底路径用它判断"还需不需要补"。
_ROW_LIMIT_PATTERN = re.compile(r"\b(LIMIT|FETCH|ROWNUM|TOP)\b", re.IGNORECASE)
_AGGREGATE_ONLY_PATTERN = re.compile(r"\bCOUNT\s*\(\s*\*", re.IGNORECASE)
_GROUP_BY_PATTERN = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
_SELECT_STAR_PATTERN = re.compile(r"\bSELECT\s+\*", re.IGNORECASE)

#: 保守兜底用的表名正则：引号形式与裸标识符都认，并允许 schema 限定。
_TABLE_REF_PATTERN = re.compile(
    r"\b(?:FROM|JOIN)\s+"
    r"(?P<name>(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)"
    r"(?:\s*\.\s*(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*))*)"
    r"(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_][\w$]*))?",
    re.IGNORECASE,
)

#: 加了限流之后仍要保留的可比对面（用于确认 AST 重写没改坏查询）。
_STRUCTURE_SQL_ARGS = ("from", "joins", "where", "group", "having", "order", "qualify")


@dataclass(slots=True, frozen=True)
class TableRef:
    """SQL 里引用到的一张表。"""

    name: str
    schema: str | None = None
    alias: str | None = None
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
    #: 是否走了"解析失败 -> 保守正则"的兜底路径。
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

    _check_read_only(dialect, cleaned, result)
    if result.errors:
        return result

    limited_sql, limited, fell_back = _apply_row_limit(dialect, cleaned, row_limit)
    result.sql = limited_sql
    result.limited = limited
    result.fell_back = fell_back
    if fell_back:
        result.warnings.append("SQL 无法被解析器识别，已按保守策略处理（未做结构校验）")

    refs = extract_table_refs(dialect, result.sql)
    result.tables = refs

    if fell_back:
        # 解析失败时只做**表范围**检查（这是安全控制），
        # 存在性检查降级为提示 —— 解析不准的错误不能变成硬拒绝。
        _check_scope_only(refs, scope, result)
    else:
        _check_tables(refs, snapshot, scope, result)
        _check_columns(result.sql, dialect, refs, snapshot, result)

    if _SELECT_STAR_PATTERN.search(mask_literals(result.sql)):
        result.warnings.append("使用了 SELECT *，建议只选择需要的列以提高效率")

    return result


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
        if word.upper() in _SCALAR_FUNCTION_WORDS and haystack[match.end() :].lstrip().startswith("("):
            continue
        result.errors.append(f"语句包含被禁止的模式: {word}")
        return


def _check_scope_only(
    refs: list[TableRef], scope: CatalogScope | None, result: GuardResult
) -> None:
    """兜底路径：只强制表筛选范围。"""
    if scope is None or scope.tables is None:
        return
    for ref in refs:
        if ref.is_cte or ref.is_system:
            continue
        if not scope.allows(ref.name):
            result.errors.append(
                f"表 '{ref.name}' 不在允许的表筛选列表中。允许的表: "
                f"{', '.join(sorted(scope.tables))}"
            )


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
            result.errors.append(
                f"表 '{ref.name}' 不存在。可用表: {', '.join(available)}{suffix}"
            )


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

    优先走 AST（`find_all(exp.Table)`）：能正确处理引号形式、schema 限定、
    CTE 遮蔽与别名。解析失败时走保守正则，只认它认得准的部分。
    """
    tree = _parse(dialect, sql)
    if tree is None:
        return _regex_table_refs(dialect, sql)

    cte_names = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    refs: list[TableRef] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for node in tree.find_all(exp.Table):
        name = node.name
        if not name:
            continue
        schema = node.db or None
        alias = node.alias or None
        key = (name.lower(), schema.lower() if schema else None, alias.lower() if alias else None)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            TableRef(
                name=name,
                schema=schema,
                alias=alias,
                is_cte=name.lower() in cte_names and not schema,
                is_system=_is_system(dialect, name, schema),
            )
        )
    return refs


def _regex_table_refs(dialect: Dialect, sql: str) -> list[TableRef]:
    """保守兜底：只认 `FROM/JOIN <标识符>` 里的第一个标识符。

    认不准的（子查询、逗号连接的表清单）宁可不认 —— 漏检比误杀安全。
    """
    from dbchat.db.sqltext import split_qualified_name

    haystack = mask_literals(strip_comments(sql))
    refs: list[TableRef] = []
    seen: set[str] = set()
    for match in _TABLE_REF_PATTERN.finditer(haystack):
        raw = match.group("name")
        schema, name = split_qualified_name(raw)
        if not name or name.upper() in _SQL_NOISE:
            continue
        key = f"{schema}.{name}".lower()
        if key in seen:
            continue
        seen.add(key)
        alias = match.group("alias")
        refs.append(
            TableRef(
                name=name,
                schema=schema,
                alias=alias,
                is_system=_is_system(dialect, name, schema),
            )
        )
    return refs


#: 不该被当成表名的关键字（`FROM (SELECT ...)` / `JOIN LATERAL` 之类）。
_SQL_NOISE = frozenset(
    {"select", "lateral", "unnest", "values", "generate_series", "json_table", "dual"}
)


def _is_system(dialect: Dialect, name: str, schema: str | None) -> bool:
    """系统对象判定：schema 限定走方言，未限定走共享名单。"""
    if schema and schema.lower() in {s.lower() for s in dialect.system_schemas}:
        return True
    return name.lower() in SYSTEM_RELATION_NAMES


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
        # 安全阀：解析失败绝不等于拒绝执行。退回文本追加，
        # 追加用的仍是方言自己的 limit_clause（达梦得到 FETCH FIRST）。
        logger.warning("sqlglot 无法解析该 SQL，改用文本方式补行数上限: %s", sql[:200])
        if _has_limit_text(sql) or _is_aggregate_only(sql):
            return sql, False, True
        return dialect.apply_row_limit(sql, row_limit), True, True

    if not _needs_limit(tree, sql):
        return sql, False, False

    try:
        rewritten = tree.limit(row_limit).sql(dialect=dialect.sqlglot_name)
    except Exception as exc:  # noqa: BLE001 - 重渲染失败退文本路径，不放弃查询
        logger.warning("AST 补行数上限失败，改用文本方式: %s: %s", type(exc).__name__, exc)
        return dialect.apply_row_limit(sql, row_limit), True, True

    if not _same_query(tree, dialect, rewritten):
        # 重渲染把查询改样了（sqlglot 对某些方言语法会做有损转换）：
        # 宁可退回"原句 + 限流子句"，也不要执行一条被悄悄改写的 SQL。
        logger.warning("AST 重渲染改变了查询结构，退回文本方式追加限流: %s", sql[:200])
        return dialect.apply_row_limit(sql, row_limit), True, False
    return rewritten, True, False


def _parse(dialect: Dialect, sql: str) -> exp.Expression | None:
    """解析 SQL；失败返回 None（并且只记 warning，不抛）。"""
    try:
        return sqlglot.parse_one(sql, read=dialect.sqlglot_name)
    except ParseError as exc:
        logger.warning("sqlglot 解析失败 (%s): %s", dialect.sqlglot_name, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 解析器内部异常同样不能变成拒绝
        logger.warning("sqlglot 解析异常 (%s): %s: %s", dialect.sqlglot_name, type(exc).__name__, exc)
        return None


def _needs_limit(tree: exp.Expression, sql: str) -> bool:
    """是否需要补限流。"""
    if not isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)):
        # SHOW / EXPLAIN / DESCRIBE 之类不需要（也不能）加 LIMIT。
        return False
    if tree.args.get("limit") is not None or tree.args.get("offset") is not None:
        return False
    if isinstance(tree, exp.Select) and (tree.args.get("top") is not None):
        return False
    if _has_limit_text(sql):
        # 形如 `WHERE ROWNUM <= 10` 的写法 sqlglot 不一定建模成 limit。
        return False
    return not _is_aggregate_only(sql)


def _has_limit_text(sql: str) -> bool:
    return bool(_ROW_LIMIT_PATTERN.search(mask_literals(sql)))


def _is_aggregate_only(sql: str) -> bool:
    """`SELECT COUNT(*) FROM t` 这类天然只返回一行，不必再限流。"""
    haystack = mask_literals(sql)
    return bool(_AGGREGATE_ONLY_PATTERN.search(haystack)) and not _GROUP_BY_PATTERN.search(haystack)


def _same_query(before: exp.Expression, dialect: Dialect, rewritten: str) -> bool:
    """确认重写后的 SQL 与原查询是同一个查询（只多了限流）。"""
    after = _parse(dialect, rewritten)
    if after is None:
        return False
    return _fingerprint(before) == _fingerprint(after)


def _fingerprint(tree: exp.Expression) -> tuple[Any, ...]:
    """结构指纹：表集合 + 列集合 + 各子句的规范化文本。"""
    tables = sorted(node.sql() for node in tree.find_all(exp.Table))
    columns = sorted(node.sql() for node in tree.find_all(exp.Column))
    clauses = tuple(
        tree.args[key].sql() if tree.args.get(key) is not None else ""
        for key in _STRUCTURE_SQL_ARGS
    )
    return (tuple(tables), tuple(columns), clauses)
