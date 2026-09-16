"""SQL validator — ensures only read-only queries are executed, validates schema references.

Supports both MySQL and PostgreSQL syntax.
"""

import re
from typing import Optional


# Allowed statement types (MySQL + PostgreSQL)
ALLOWED_COMMANDS = {"SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH", "TABLE"}

# Dangerous patterns that indicate write/modification intent
FORBIDDEN_PATTERNS = [
    r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bDROP\b",
    r"\bCREATE\b", r"\bALTER\b", r"\bTRUNCATE\b", r"\bRENAME\b",
    r"\bGRANT\b", r"\bREVOKE\b", r"\bREPLACE\b", r"\bLOAD\b",
    r"\bCALL\b", r"\bEXEC\b", r"\bEXECUTE\b", r"\bPREPARE\b",
    r"\bFLUSH\b", r"\bSET\b", r"\bLOCK\b", r"\bUNLOCK\b",
    r"\bKILL\b", r"\bSHUTDOWN\b", r"\bHANDLER\b",
    r"\bINTO\s+(OUTFILE|DUMPFILE)\b",
    r"\bSLEEP\s*\(.*\)", r"\bBENCHMARK\s*\(.*\)",
    r"\bWAITFOR\s+DELAY\b",
    # PostgreSQL specific
    r"\bVACUUM\b", r"\bREINDEX\b", r"\bDISCARD\b",
    r"\bCOPY\b", r"\bNOTIFY\b", r"\bLISTEN\b",
    r"\bREFRESH\s+MATERIALIZED\b",
    r"\bpg_sleep\s*\(.*\)",
    r"\bpg_read_file\b", r"\bpg_write_file\b",
    r"\bpg_read_binary_file\b",
    r"\blo_import\b", r"\blo_export\b",
]

# Row-limiting keywords — queries with any of these don't need auto-LIMIT
ROW_LIMIT_KEYWORDS = {"LIMIT", "OFFSET", "FETCH", "ROWNUM", "TOP"}


class SQLValidationError(Exception):
    """Raised when a SQL statement fails validation."""
    pass


def validate_sql(sql: str, db_type: str = "mysql") -> str:
    """
    Validate that a SQL statement is read-only and safe to execute.

    Args:
        sql: The SQL statement to validate.
        db_type: 'mysql' or 'postgresql' — affects allowed commands.

    Returns the cleaned SQL if valid.
    Raises SQLValidationError if the statement is not allowed.
    """
    if not sql or not sql.strip():
        raise SQLValidationError("Empty SQL statement")

    # Remove comments
    cleaned = _strip_comments(sql.strip())
    cleaned = cleaned.strip().rstrip(";").strip()

    if not cleaned:
        raise SQLValidationError("Empty SQL after stripping comments")

    upper_sql = cleaned.upper()
    first_word = upper_sql.split()[0] if upper_sql.split() else ""

    # Backend-specific: reject foreign SQL dialects
    if db_type in ("postgresql", "dameng", "dm", "dm8"):
        if first_word in ("SHOW", "DESCRIBE", "DESC", "HANDLER"):
            raise SQLValidationError(
                f"'{first_word}' 是 MySQL 命令，当前数据库不支持。"
                f"请使用 SELECT 查询系统表。"
            )
    elif db_type in ("mysql", "mariadb"):
        if first_word in ("VACUUM", "REINDEX", "ANALYZE"):
            raise SQLValidationError(f"'{first_word}' 是 PostgreSQL 命令，MySQL 不支持。")

    # Check for forbidden patterns
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, upper_sql):
            raise SQLValidationError(
                f"SQL statement contains forbidden pattern: {pattern}"
            )

    # Check that first word is an allowed command
    if first_word not in ALLOWED_COMMANDS:
        raise SQLValidationError(
            f"Statement type '{first_word}' is not allowed. "
            f"Only {', '.join(sorted(ALLOWED_COMMANDS))} are permitted."
        )

    # Check for multi-statement (semicolons outside of strings)
    if _has_multi_statement(cleaned):
        raise SQLValidationError("Multi-statement queries are not allowed")

    # Auto-append LIMIT 200 for SELECT queries without row-limiting
    cleaned = _enforce_row_limit(cleaned, first_word)

    return cleaned


def _enforce_row_limit(sql: str, first_word: str) -> str:
    """Auto-append LIMIT 200 to SELECT queries that lack row-limiting clauses.

    Skips: COUNT(*) queries, aggregate-only queries, and queries that already
    have LIMIT, OFFSET, FETCH, ROWNUM, or TOP.
    """
    if first_word != "SELECT":
        return sql

    upper = sql.upper()

    # Skip if already has a row-limiting clause
    for kw in ROW_LIMIT_KEYWORDS:
        if re.search(r'\b' + kw + r'\b', upper):
            return sql

    # Skip COUNT(*) or aggregate-only queries (they return 1 row naturally)
    if re.search(r'\bCOUNT\s*\(\s*\*', upper) and not re.search(r'\bGROUP\s+BY\b', upper):
        return sql

    # Append LIMIT 200 as a safety net
    return sql.rstrip() + " LIMIT 200"


def _strip_comments(sql: str) -> str:
    """Remove single-line and multi-line comments from SQL."""
    # Remove multi-line comments /* ... */
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)
    # Remove single-line comments -- ...
    sql = re.sub(r'--[^\n]*', '', sql)
    # Remove single-line comments # ...
    lines = []
    for line in sql.split('\n'):
        # Only strip # that's not inside a string
        if '#' in line:
            in_string = False
            quote_char = None
            result = []
            i = 0
            while i < len(line):
                ch = line[i]
                if not in_string and ch in ("'", '"'):
                    in_string = True
                    quote_char = ch
                elif in_string and ch == quote_char and (i == 0 or line[i-1] != '\\'):
                    in_string = False
                    quote_char = None
                elif not in_string and ch == '#':
                    break
                result.append(ch)
                i += 1
            line = ''.join(result)
        lines.append(line)
    return '\n'.join(lines)


def _has_multi_statement(sql: str) -> bool:
    """Check if SQL contains semicolons outside of string literals."""
    in_single = False
    in_double = False
    for i, ch in enumerate(sql):
        if ch == "'" and not in_double:
            if i > 0 and sql[i-1] == '\\':
                continue
            in_single = not in_single
        elif ch == '"' and not in_single:
            if i > 0 and sql[i-1] == '\\':
                continue
            in_double = not in_double
        elif ch == ';' and not in_single and not in_double:
            return True
    return False


def validate_schema_references(sql: str, snapshot: Optional[object] = None,
                                table_filter: list = None) -> dict:
    """Pre-flight check: validate that tables and columns in SQL exist in schema.

    Args:
        sql: The SQL query to validate.
        snapshot: SchemaSnapshot with .tables (dict of name → TableMeta).
        table_filter: Optional list of allowed table names.

    Returns:
        {"valid": bool, "errors": [str], "warnings": [str]}
        Errors are blocking (query should not execute).
        Warnings are informational (query may work but looks suspicious).
    """
    if snapshot is None:
        return {"valid": True, "errors": [], "warnings": []}

    errors = []
    warnings = []
    tables_dict = getattr(snapshot, 'tables', {}) or {}
    allowed_set = set(table_filter) if table_filter else None

    if not tables_dict:
        return {"valid": True, "errors": [], "warnings": []}

    # Extract table references from FROM and JOIN clauses
    # Matches: FROM table, JOIN table, FROM table AS alias, etc.
    table_refs_raw = re.findall(
        r'(?:FROM|JOIN)\s+`?(\w+)`?(?:\s+(?:AS\s+)?`?(\w+)`?)?',
        sql, re.IGNORECASE
    )
    # table_refs_raw is list of (table_name, alias_or_empty) tuples

    # Build alias → real_table mapping
    alias_map = {}
    referenced_tables = set()
    for tbl, alias in table_refs_raw:
        tbl_lower = tbl.lower()
        referenced_tables.add(tbl_lower)
        if alias:
            alias_map[alias.lower()] = tbl_lower
        else:
            alias_map[tbl_lower] = tbl_lower

    # Check table existence (skip system tables/views)
    for tbl in referenced_tables:
        if tbl.lower() in _SYSTEM_TABLES:
            continue  # System view — always allowed
        if tbl not in tables_dict:
            # Try case-insensitive match
            match = None
            for tname in tables_dict:
                if tname.lower() == tbl:
                    match = tname
                    break
            if match:
                alias_map[tbl] = match.lower()
            else:
                available = list(tables_dict.keys())[:10]
                errors.append(
                    f"表 '{tbl}' 不存在。可用表: {', '.join(available)}"
                    + ("..." if len(tables_dict) > 10 else "")
                )

    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}

    # Check table filter — only allow queries on selected tables (skip system views)
    if allowed_set is not None:
        for tbl in referenced_tables:
            if tbl.lower() in _SYSTEM_TABLES:
                continue  # System view — always allowed
            if tbl not in allowed_set:
                # Try case-insensitive match against allowed set
                match = None
                for at in allowed_set:
                    if at.lower() == tbl:
                        match = at
                        break
                if not match:
                    errors.append(
                        f"表 '{tbl}' 不在允许的表筛选列表中。"
                        f"允许的表: {', '.join(sorted(allowed_set))}"
                    )
        if errors:
            return {"valid": False, "errors": errors, "warnings": warnings}

    # Extract column references from SELECT list, WHERE, GROUP BY, ORDER BY, HAVING only
    # (FROM/JOIN clauses contain table references, not columns — skip those)
    clauses = _extract_sql_clauses(sql, referenced_tables)

    col_refs_raw = re.findall(
        r'(?:`?(\w+)`?\.)?`?(\w+)`?',
        clauses, re.IGNORECASE
    )
    # Each match is (table_qualifier_or_empty, column_name)

    for qualifier, col_name in col_refs_raw:
        col_lower = col_name.lower()

        # Skip SQL keywords and common expressions
        if col_lower in _SQL_KEYWORDS:
            continue

        # If qualified, check the column exists in that specific table
        if qualifier:
            qual_lower = qualifier.lower()
            real_table = alias_map.get(qual_lower, qual_lower)
            meta = tables_dict.get(real_table) or next(
                (tables_dict[t] for t in tables_dict if t.lower() == real_table), None
            )
            if meta:
                col_names = {c.name.lower() for c in meta.columns}
                if col_lower not in col_names:
                    available_cols = [c.name for c in meta.columns[:8]]
                    errors.append(
                        f"列 '{qualifier}.{col_name}' 不存在于表 '{real_table}'。"
                        f"可用列: {', '.join(available_cols)}"
                        + ("..." if len(meta.columns) > 8 else "")
                    )
        else:
            # Unqualified column — check if it exists in ANY referenced table
            found = False
            for tbl in referenced_tables:
                meta = tables_dict.get(tbl) or next(
                    (tables_dict[t] for t in tables_dict if t.lower() == tbl), None
                )
                if meta:
                    col_names = {c.name.lower() for c in meta.columns}
                    if col_lower in col_names:
                        found = True
                        break
            if not found and referenced_tables:
                warnings.append(
                    f"列 '{col_name}' 在引用表中未找到，可能是 SQL 函数或别名"
                )

    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}

    # Check for SELECT * on tables with many columns (efficiency warning)
    if re.search(r'\bSELECT\s+\*(?:\s|,|$)', sql, re.IGNORECASE):
        warnings.append("使用了 SELECT *，建议只选择需要的列以提高效率")

    return {"valid": True, "errors": [], "warnings": warnings}


def _extract_sql_clauses(sql: str, table_names: set) -> str:
    """Extract non-FROM parts of SQL for column validation.

    Removes FROM/JOIN clauses to avoid flagging table names as missing columns.
    """
    # Keep SELECT ... WHERE ... GROUP BY ... ORDER BY ... HAVING parts
    # Remove FROM ... up to WHERE/GROUP/ORDER/LIMIT/HAVING
    upper = sql.upper()
    # Find WHERE, GROUP BY, ORDER BY, HAVING, LIMIT positions
    cut_keywords = []
    for kw in [' WHERE ', ' GROUP BY ', ' ORDER BY ', ' HAVING ', ' LIMIT ']:
        pos = upper.find(kw)
        if pos > 0:
            cut_keywords.append((pos, kw))
    if not cut_keywords:
        # No clauses after FROM — the whole thing is SELECT + FROM
        # Just return the SELECT part (before FROM)
        from_pos = upper.find(' FROM ')
        if from_pos > 0:
            return sql[:from_pos]
        return sql
    # Return SELECT ... FROM table part (up to first cut keyword) + everything after
    first_cut = min(pos for pos, _ in cut_keywords)
    # Also remove FROM up to first cut
    from_pos = upper.find(' FROM ')
    if from_pos > 0:
        return sql[:from_pos] + ' ' + sql[first_cut:]
    return sql


# System table/view names that should never be flagged as "table not found"
_SYSTEM_TABLES = {
    # Oracle/Dameng system views
    "all_tables", "all_tab_columns", "all_tab_comments", "all_col_comments",
    "all_constraints", "all_cons_columns", "all_indexes", "all_ind_columns",
    "all_views", "all_users", "all_objects", "all_sequences",
    "user_tables", "user_tab_columns", "user_constraints", "user_cons_columns",
    "user_indexes", "user_ind_columns", "user_views", "user_users", "user_objects",
    "dba_tables", "dba_tab_columns", "dba_constraints",
    "v$version", "v$instance", "v$database", "v$session",
    # MySQL system tables/views
    "information_schema", "mysql", "performance_schema", "sys",
    # PostgreSQL system tables/views
    "pg_catalog", "pg_tables", "pg_indexes", "pg_class", "pg_namespace",
    "pg_attribute", "pg_constraint", "pg_stat_user_tables",
    "information_schema.tables", "information_schema.columns",
    "information_schema.key_column_usage", "information_schema.table_constraints",
    "information_schema.referential_constraints", "information_schema.statistics",
    # SQLite
    "sqlite_master", "sqlite_sequence",
}

# SQL keywords that shouldn't trigger "column not found" warnings
_SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "in", "is", "null",
    "like", "between", "join", "inner", "outer", "left", "right", "full",
    "on", "as", "asc", "desc", "order", "by", "group", "having", "limit",
    "offset", "union", "all", "distinct", "case", "when", "then", "else",
    "end", "exists", "any", "some", "true", "false", "count", "sum", "avg",
    "min", "max", "coalesce", "cast", "convert", "if", "ifnull", "nullif",
    "now", "curdate", "curtime", "current_date", "current_time",
    "current_timestamp", "date", "time", "timestamp", "year", "month", "day",
    "hour", "minute", "second", "extract", "date_format", "datediff",
    "concat", "substring", "trim", "upper", "lower", "length", "replace",
    "round", "floor", "ceil", "abs", "mod", "floor", "power", "sqrt",
    "rownum", "fetch", "first", "next", "rows", "only", "top", "percent",
    "partition", "over", "row_number", "rank", "dense_rank", "ntile",
    "lead", "lag", "first_value", "last_value",
    # PostgreSQL specific
    "ilike", "similar", "regexp", "interval", "boolean", "integer",
    "varchar", "text", "numeric", "serial", "bigserial",
}
