"""SQL query executor with validation, schema checking, and error handling."""

from typing import Optional

from app.database.connection import db
from app.database.sql_validator import validate_sql, validate_schema_references, SQLValidationError


class QueryExecutionError(Exception):
    """Raised when query execution fails."""
    def __init__(self, message: str, sql: str = "", mysql_error: str = "",
                 schema_errors: list = None):
        self.message = message
        self.sql = sql
        self.mysql_error = mysql_error
        self.schema_errors = schema_errors or []
        super().__init__(message)


def execute_query(sql: str, max_rows: int = 200, snapshot: object = None,
                  table_filter: list = None) -> dict:
    """
    Validate and execute a read-only SQL query.

    Args:
        sql: The SQL SELECT statement to execute.
        max_rows: Maximum number of rows to return.
        snapshot: Optional SchemaSnapshot for pre-flight column/table validation.
        table_filter: Optional list of allowed table names.

    Returns:
        dict with keys: columns, rows, row_count, execution_ms.

    Raises:
        QueryExecutionError: If validation or execution fails.
    """
    # Step 1: Validate SQL syntax and security
    try:
        cleaned_sql = validate_sql(sql, db.db_type)
    except SQLValidationError as e:
        raise QueryExecutionError(
            message=f"SQL validation failed: {e}",
            sql=sql,
        ) from e

    # Step 2: Pre-flight schema reference check (column/table existence)
    if snapshot is not None:
        schema_check = validate_schema_references(cleaned_sql, snapshot, table_filter)
        if not schema_check["valid"]:
            error_detail = "; ".join(schema_check["errors"])
            raise QueryExecutionError(
                message=f"Schema validation failed: {error_detail}",
                sql=cleaned_sql,
                schema_errors=schema_check["errors"],
            )
        # Attach warnings to result (non-blocking)
        schema_warnings = schema_check.get("warnings", [])
    else:
        schema_warnings = []

    # Step 3: Execute
    try:
        result = db.execute(cleaned_sql)
    except Exception as e:
        raise QueryExecutionError(
            message=f"Database query failed: {e}",
            sql=cleaned_sql,
            mysql_error=str(e),
        ) from e

    # Attach schema warnings if any
    if schema_warnings:
        result["schema_warnings"] = schema_warnings

    # Step 3: Truncate if too many rows
    if result["row_count"] > max_rows:
        result["rows"] = result["rows"][:max_rows]
        result["truncated"] = True
        result["truncated_message"] = (
            f"Results truncated to {max_rows} rows. "
            f"Total matches: {result['row_count']}."
        )

    return result


def format_query_result(result: dict) -> str:
    """Format query result for display in the LLM context."""
    if result["row_count"] == 0:
        return "查询结果为空，没有找到匹配的记录。"

    lines = []
    lines.append(f"查询返回 {result['row_count']} 行，耗时 {result['execution_ms']}ms。")
    lines.append("")

    # Column headers
    if result["columns"]:
        lines.append(" | ".join(result["columns"]))
        lines.append("-" * len(" | ".join(result["columns"])))

        for row in result["rows"]:
            formatted = []
            for val in row:
                if val is None:
                    formatted.append("NULL")
                else:
                    formatted.append(str(val))
            lines.append(" | ".join(formatted))

    if result.get("truncated"):
        lines.append("")
        lines.append(result["truncated_message"])

    return "\n".join(lines)
