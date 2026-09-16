"""数据查询工具 —— 唯一会执行用户（模型）SQL 的工具。

这里同时修掉一个真实缺陷：界面上"共 N 次查询"的数字历史恒为 0。
`app/agent/orchestrator.py:354` 判断的是 `event["type"] == "tool_use"`，
那是 **LLM 客户端层**的事件名，而 agent 层投递的是 `tool_call`，
条件永不成立。现在由本工具直接按工具名计数（`RunContext.add_query_result`），
不再依赖任何事件名的巧合。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from dbchat.db.dialects.base import Dialect
from dbchat.domain.errors import DbError, ValidationError
from dbchat.domain.query import QueryResult
from dbchat.tools.registry import ToolContext, ToolOutcome

logger = logging.getLogger(__name__)

__all__ = ["QueryDatabaseParams", "format_query_result", "query_database"]


class QueryDatabaseParams(BaseModel):
    """`query_database` 入参。"""

    sql: str = Field(description="要执行的只读 SQL 查询语句")


def query_tool_description(dialect: Dialect | None) -> str:
    """按方言渲染工具描述。

    历史实现把"标准 MySQL 语法，表名和列名用反引号"写死在工具定义里
    （`app/agent/tools.py:389-402` 与 `:641-653`），于是连到 PostgreSQL
    或达梦时，模型收到的仍是 MySQL 的语法提示 —— SQL 里带反引号，PG 直接
    报语法错误。这里改为渲染 `Dialect.sql_dialect_hint()` 与 `prompt_notes()`。
    """
    hint = (
        dialect.sql_dialect_hint()
        if dialect is not None
        else "表名和列名请按当前数据库的引用规则书写。"
    )
    notes = dialect.prompt_notes() if dialect is not None else ()
    lines = [
        f"对当前连接的 {dialect.display_name if dialect else ''} 数据库执行只读 SQL 查询。"
        "使用此工具回答关于实际数据的问题：谁、什么、何时、多少人、具体记录、统计数据等。"
        "只允许 SELECT 语句。",
        hint,
        "可使用 JOIN、WHERE、GROUP BY、ORDER BY 等；必须限制返回行数。",
    ]
    if notes:
        lines.append("方言注意事项：" + "；".join(notes[:4]))
    return "\n".join(lines)


async def query_database(params: QueryDatabaseParams, ctx: ToolContext) -> ToolOutcome:
    """校验并执行一条只读 SQL。"""
    sql = (params.sql or "").strip()
    if not sql:
        return ToolOutcome.failure("未提供 SQL 查询语句", content="错误：未提供 SQL 查询语句。")

    try:
        async with ctx.database() as executor:
            result = await executor.query(sql, snapshot=ctx.snapshot, scope=ctx.scope)
    except ValidationError as exc:
        # 校验未通过（写操作、表不存在、不在筛选范围…）—— 把原因回给模型，
        # 它下一轮就能改。带上 SQL 是为了线上事件的 sql_preview（默认不展示）。
        logger.info("查询未通过校验: %s | SQL: %s", exc.detail or exc.user_message, sql[:300])
        return ToolOutcome(
            ok=False,
            summary="查询未通过校验",
            content=f"查询无法执行：{exc.user_message}",
            payload={"sql": sql, "error_detail": exc.detail or exc.user_message},
            error_detail=exc.detail or exc.user_message,
        )
    except DbError as exc:
        logger.info("查询执行失败: %s | SQL: %s", exc.detail or exc.user_message, sql[:300])
        return ToolOutcome(
            ok=False,
            summary="查询执行失败",
            content=f"查询执行失败：{exc.user_message}",
            payload={"sql": sql, "error_detail": exc.detail or exc.user_message},
            error_detail=exc.detail or exc.user_message,
        )

    # ---- 缺陷修复：查询计数由工具本身完成 ----
    ctx.ctx.add_query_result(result)

    return ToolOutcome(
        ok=True,
        summary=_summary(result),
        content=format_query_result(result),
        payload={
            "sql": sql,
            "row_count": result.row_count,
            "execution_ms": result.execution_ms,
            "truncated": result.truncated,
            "columns": result.columns,
            "rows": result.rows,
        },
    )


def _summary(result: QueryResult) -> str:
    if result.row_count == 0:
        return "查询成功，没有匹配的数据"
    suffix = "（已截断）" if result.truncated else ""
    return f"返回 {result.row_count} 行{suffix}"


def format_query_result(result: QueryResult) -> str:
    """把结果渲染成给模型看的文本。空结果与"没执行查询"必须能区分开。"""
    if not result.columns:
        return "查询已执行，但该语句没有返回结果集。"
    if result.row_count == 0:
        return "查询执行成功，但没有匹配的数据（0 行）。"

    lines = [
        f"查询返回 {result.row_count} 行，耗时 {result.execution_ms}ms。",
        "",
        " | ".join(result.columns),
        "-" * min(len(" | ".join(result.columns)), 80),
    ]
    for row in result.rows:
        lines.append(" | ".join(_stringify(row.get(column)) for column in result.columns))
    if result.truncated:
        lines.append("")
        lines.append("结果已被行数上限截断，如需完整数据请加上聚合或更精确的筛选条件。")
    for notice in result.notices:
        lines.append(f"提示：{notice}")
    return "\n".join(lines)


def _stringify(value: object) -> str:
    if value is None:
        return "NULL"
    text = str(value)
    return text if len(text) <= 200 else text[:197] + "..."
