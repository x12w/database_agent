"""结构探索类工具 —— 全部以 `SchemaSnapshot` 为准。

历史实现对每个工具都现拼一条 `information_schema` 查询（`list_tables`、
`describe_table`、`sample_data` 各一份），并且按库类型三向分支；同样一件事
在内省层已经做过一次，两处结果还可能不一致。

这里改成读快照：`ctx.snapshot` 是本进程刚刚内省出来的结构，既有列类型、
注释、索引，也有抽样行与取值画像（`ColumnMeta.value_type`）。真正需要
访问数据库的只剩"补一次抽样"这一条路径。
"""

from __future__ import annotations

import logging
import re
from collections import deque
from typing import Any

from pydantic import BaseModel, Field

from dbchat.domain.schema import CatalogScope, SchemaSnapshot, TableMeta
from dbchat.tools.registry import ToolContext, ToolOutcome

logger = logging.getLogger(__name__)

__all__ = [
    "AnalyzeColumnParams",
    "DescribeTableParams",
    "EmptyParams",
    "FindJoinPathParams",
    "FindRelatedParams",
    "SampleDataParams",
    "TableNameParams",
    "analyze_column",
    "describe_table",
    "extract_suggestions",
    "find_join_path",
    "find_related_tables",
    "get_schema_summary",
    "get_suggested_questions",
    "list_tables",
    "sample_data",
]


# ---------------------------------------------------------------- 入参模型


class EmptyParams(BaseModel):
    """无入参工具。"""


class TableNameParams(BaseModel):
    """单表工具入参。"""

    table_name: str = Field(description="表名")


class DescribeTableParams(BaseModel):
    """查看表结构。"""

    table_name: str = Field(description="要查看的表名")


class SampleDataParams(BaseModel):
    """抽样入参。"""

    table_name: str = Field(description="要抽样的表名")
    limit: int = Field(default=5, ge=1, le=50, description="返回的行数（默认 5）")


class AnalyzeColumnParams(BaseModel):
    """列分析入参。"""

    table_name: str = Field(description="表名")
    column_name: str = Field(description="列名")


class FindRelatedParams(BaseModel):
    """关联表查询入参。"""

    table_name: str = Field(description="表名")


class FindJoinPathParams(BaseModel):
    """多跳路径查询入参。"""

    source_table: str = Field(description="起始表名")
    target_table: str = Field(description="目标表名")


# ---------------------------------------------------------------- 公共辅助


def _snapshot(ctx: ToolContext) -> SchemaSnapshot | None:
    return ctx.snapshot


def _missing_snapshot() -> ToolOutcome:
    return ToolOutcome.failure(
        "数据库结构尚未加载完成",
        content="数据库结构尚未加载完成，请稍后重试或先用其他方式确认表结构。",
        detail="RunContext.snapshot is None",
    )


def _scoped_tables(snapshot: SchemaSnapshot, scope: CatalogScope) -> dict[str, TableMeta]:
    """按表筛选范围裁剪。"""
    if scope.tables is None:
        return dict(snapshot.tables)
    return {name: meta for name, meta in snapshot.tables.items() if scope.allows(name)}


def _lookup(tables: dict[str, TableMeta], name: str) -> TableMeta | None:
    if name in tables:
        return tables[name]
    lowered = name.lower()
    for key, meta in tables.items():
        if key.lower() == lowered:
            return meta
    return None


def _quote(ctx: ToolContext, ident: str) -> str:
    dialect = ctx.dialect
    return dialect.quote(ident) if dialect is not None else ident


def _sample_columns(meta: TableMeta, limit: int = 6) -> list[str]:
    if not meta.sample_rows:
        return []
    return list(meta.sample_rows[0].keys())[:limit]


def _render_sample_table(meta: TableMeta, columns: list[str], rows: int = 3) -> list[str]:
    if not meta.sample_rows or not columns:
        return []
    lines = [f"抽样 ({len(meta.sample_rows)} 行):"]
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "|".join(["---"] * len(columns)) + "|")
    for row in meta.sample_rows[:rows]:
        values = [
            "NULL" if row.get(column) is None else str(row.get(column))[:25] for column in columns
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def _index_summary(meta: TableMeta) -> str:
    """索引摘要 —— 大表查询能否走索引全靠它。"""
    if not meta.indexes:
        return ""
    parts: list[str] = []
    for index in meta.indexes:
        columns = ",".join(index.columns) or index.name
        parts.append(f"⚡`{columns}`(唯一)" if index.unique else f"`{columns}`")
    return ", ".join(parts[:8])


# ---------------------------------------------------------------- 工具实现


async def get_schema_summary(params: EmptyParams, ctx: ToolContext) -> ToolOutcome:
    """完整结构概览 —— 一次调用替代多次 list_tables/describe_table/sample_data。"""
    snapshot = _snapshot(ctx)
    if snapshot is None:
        return _missing_snapshot()

    tables = _scoped_tables(snapshot, ctx.scope)
    foreign_keys = [
        fk
        for fk in snapshot.foreign_keys
        if fk.table in tables and (ctx.scope.tables is None or ctx.scope.allows(fk.ref_table))
    ]

    lines = [
        f"# 数据库: {snapshot.db_name}",
        f"表数: {len(tables)} | 外键: {len(foreign_keys)}\n",
        "## 表概览",
        "| 表名 | 行数 | 列数 | 说明 |",
        "|------|------|------|------|",
    ]
    for name, meta in tables.items():
        lines.append(f"| {name} | {meta.row_count if meta.row_count is not None else '?'} | {len(meta.columns)} | {meta.comment or '-'} |")
    lines.append("")

    for name, meta in tables.items():
        lines.append(f"## {name}（{meta.row_count if meta.row_count is not None else '?'} 行）")
        lines.append("| 字段 | 类型 | 可空 | 键 | 说明 |")
        lines.append("|------|------|------|----|------|")
        for column in meta.columns:
            key = "🔑" if column.is_primary_key else "-"
            lines.append(
                f"| {column.name} | {column.column_type or column.data_type} | "
                f"{'是' if column.nullable else '否'} | {key} | {column.comment or '-'} |"
            )
        indexes = _index_summary(meta)
        if indexes:
            lines.append(f"\n📌 索引: {indexes}")
        lines.extend(_render_sample_table(meta, _sample_columns(meta)))
        lines.append("")

    if foreign_keys:
        lines.append("## 外键关系（含 JOIN 示例）")
        for fk in foreign_keys:
            parent, child = _quote(ctx, fk.table), _quote(ctx, fk.ref_table)
            lines.append(
                f"- {parent}.{_quote(ctx, fk.column)} → {child}.{_quote(ctx, fk.ref_column)}\n"
                f"  `JOIN {child} ON {parent}.{_quote(ctx, fk.column)} = {child}.{_quote(ctx, fk.ref_column)}`"
            )
        lines.append("")

    distributions = _distribution_lines(tables)
    if distributions:
        lines.extend(distributions)

    return ToolOutcome(
        ok=True,
        summary=f"{len(tables)} 张表的结构概览",
        content="\n".join(lines),
        payload={
            "tables": list(tables),
            "row_count": len(tables),
            "table_count": len(tables),
            "foreign_key_count": len(foreign_keys),
        },
    )


def _distribution_lines(tables: dict[str, TableMeta]) -> list[str]:
    """取值分布 —— 从快照的列画像里读，不额外查库。"""
    lines: list[str] = []
    for name, meta in tables.items():
        entries: list[str] = []
        for column in meta.columns:
            if column.sample_values:
                entries.append(f"  `{column.name}`: {', '.join(str(v)[:20] for v in column.sample_values[:5])}")
        if entries:
            if not lines:
                lines.append("## 值分布")
            lines.append(f"**{name}**:")
            lines.extend(entries)
    if lines:
        lines.append("")
    return lines


async def list_tables(params: EmptyParams, ctx: ToolContext) -> ToolOutcome:
    """列出当前范围内的表。"""
    snapshot = _snapshot(ctx)
    if snapshot is None:
        return _missing_snapshot()

    tables = _scoped_tables(snapshot, ctx.scope)
    if not tables:
        return ToolOutcome(
            ok=True,
            summary="没有可见的表",
            content=f"数据库 {snapshot.db_name} 在当前范围内没有表。",
            payload={"tables": [], "row_count": 0},
        )

    lines = [
        f"数据库 {snapshot.db_name} 包含以下表：",
        "| 表名 | 行数（估算） | 列数 | 注释 |",
        "|------|------------|------|------|",
    ]
    for name, meta in tables.items():
        rows = meta.row_count if meta.row_count is not None else "?"
        lines.append(f"| {name} | {rows} | {len(meta.columns)} | {meta.comment or '-'} |")
    return ToolOutcome(
        ok=True,
        summary=f"{len(tables)} 张表",
        content="\n".join(lines),
        payload={"tables": list(tables), "row_count": len(tables)},
    )


async def describe_table(params: DescribeTableParams, ctx: ToolContext) -> ToolOutcome:
    """查看单表结构（列、键、外键、行数）。"""
    snapshot = _snapshot(ctx)
    if snapshot is None:
        return _missing_snapshot()

    tables = _scoped_tables(snapshot, ctx.scope)
    meta = _lookup(tables, params.table_name)
    if meta is None:
        available = ", ".join(list(tables)[:20]) or "（无）"
        return ToolOutcome.failure(
            f"表 {params.table_name} 不可用",
            content=f"表 {params.table_name} 不在当前连接/筛选范围内。可用表: {available}",
        )

    lines = [
        f"## 表: {meta.name}",
        f"数据库: {snapshot.db_name} | 列数: {len(meta.columns)} | 行数: {meta.row_count if meta.row_count is not None else '?'}",
        "| # | 列名 | 类型 | 可空 | 键 | 默认值 | 注释 |",
        "|---|------|------|------|----|--------|------|",
    ]
    for position, column in enumerate(meta.columns, 1):
        default = "NULL" if column.default is None else str(column.default)
        lines.append(
            f"| {position} | {column.name} | {column.column_type or column.data_type} | "
            f"{'是' if column.nullable else '否'} | {'🔑' if column.is_primary_key else '-'} | "
            f"{default} | {column.comment or '-'} |"
        )
    indexes = _index_summary(meta)
    if indexes:
        lines.append(f"\n📌 索引: {indexes}")
    lines.extend(_render_sample_table(meta, _sample_columns(meta, 8), rows=3))

    foreign_keys = [fk for fk in snapshot.foreign_keys if fk.table == meta.name]
    if foreign_keys:
        lines.append("\n### 外键关系")
        for fk in foreign_keys:
            lines.append(f"- `{fk.column}` → `{fk.ref_table}`.`{fk.ref_column}`")

    rows = [
        [
            position,
            column.name,
            column.column_type or column.data_type,
            "是" if column.nullable else "否",
            "PRI" if column.is_primary_key else "",
            "NULL" if column.default is None else str(column.default),
            column.comment or "",
        ]
        for position, column in enumerate(meta.columns, 1)
    ]
    return ToolOutcome(
        ok=True,
        summary=f"{meta.name}: {len(meta.columns)} 列",
        content="\n".join(lines),
        payload={
            "table_name": meta.name,
            "columns": ["序号", "列名", "类型", "可空", "键", "默认值", "注释"],
            "rows": rows,
            "row_count": len(meta.columns),
            "foreign_keys": [f"{fk.column} → {fk.ref_table}.{fk.ref_column}" for fk in foreign_keys],
        },
    )


async def sample_data(params: SampleDataParams, ctx: ToolContext) -> ToolOutcome:
    """查看表的实际数据内容。

    优先用快照里已有的抽样行（探索阶段已经取过）；快照没有时按方言的
    行数限制语法现查一次 —— 仍然有界。
    """
    snapshot = _snapshot(ctx)
    if snapshot is None:
        return _missing_snapshot()

    tables = _scoped_tables(snapshot, ctx.scope)
    meta = _lookup(tables, params.table_name)
    if meta is None:
        return ToolOutcome.failure(f"表 {params.table_name} 不可用")

    rows = meta.sample_rows[: params.limit]
    if not rows:
        fetched = await _fetch_sample(ctx, meta.name, params.limit)
        if fetched is None:
            return ToolOutcome.failure(
                f"{meta.name} 抽样失败",
                content=f"表 {meta.name} 抽样失败（数据库不可用或不支持）。",
            )
        rows = fetched

    if not rows:
        return ToolOutcome(
            ok=True,
            summary=f"{meta.name} 没有数据",
            content=f"表 {meta.name} 中没有数据。",
            payload={"table_name": meta.name, "rows": [], "row_count": 0},
        )

    columns = list(rows[0].keys())
    lines = [f"## 表 {meta.name} 的抽样数据（{len(rows)} 行）\n"]
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "|".join(["---"] * len(columns)) + "|")
    for row in rows:
        values = ["NULL" if row.get(c) is None else str(row.get(c))[:40] for c in columns]
        lines.append("| " + " | ".join(values) + " |")
    return ToolOutcome(
        ok=True,
        summary=f"{meta.name} 抽样 {len(rows)} 行",
        content="\n".join(lines),
        payload={"table_name": meta.name, "columns": columns, "rows": rows, "row_count": len(rows)},
    )


async def _fetch_sample(ctx: ToolContext, table: str, limit: int) -> list[dict[str, Any]] | None:
    """从数据库现取若干行（快照里没有抽样时）。"""
    dialect = ctx.dialect
    sql = f"SELECT * FROM {dialect.quote(table)} {dialect.limit_clause(limit)}"
    try:
        async with ctx.database() as executor:
            result = await executor.query(sql, snapshot=ctx.snapshot, scope=ctx.scope)
    except Exception as exc:  # noqa: BLE001 - 抽样失败降级为空结果，但留日志
        logger.info("抽样 %s 失败: %s: %s", table, type(exc).__name__, exc)
        return None
    return list(result.rows)


async def analyze_column(params: AnalyzeColumnParams, ctx: ToolContext) -> ToolOutcome:
    """单列统计分析：唯一值、NULL 比例、数值区间、Top 10 分布。"""
    snapshot = _snapshot(ctx)
    if snapshot is None:
        return _missing_snapshot()

    tables = _scoped_tables(snapshot, ctx.scope)
    meta = _lookup(tables, params.table_name)
    if meta is None:
        return ToolOutcome.failure(f"表 {params.table_name} 不可用")
    column = meta.column(params.column_name)
    if column is None:
        available = ", ".join(meta.column_names()[:20])
        return ToolOutcome.failure(
            f"列 {params.column_name} 不存在",
            content=f"表 {meta.name} 中没有列 {params.column_name}。可用列: {available}",
        )

    lines = [
        f"# 列分析: {meta.name}.{column.name}",
        f"类型: {column.column_type or column.data_type} | "
        f"键: {'主键' if column.is_primary_key else '无'}",
    ]

    # 快照画像：抽样阶段已经算过，不必再查库
    if column.value_type is not None:
        lines.append(f"取值形态: {column.value_type.value}")
    if column.null_ratio is not None:
        lines.append(f"抽样空值比例: {column.null_ratio:.1%}")
    if column.sample_values:
        lines.append(f"抽样值: {', '.join(str(v)[:30] for v in column.sample_values[:5])}")

    stats = await _column_stats(ctx, meta, column.name, column.data_type, column.sample_values)
    if stats is not None:
        lines.append(
            f"总行数: {stats.get('total', '?')} | 唯一值: {stats.get('distinct', '?')} | "
            f"NULL: {stats.get('null_count', '?')}"
        )
        if stats.get("numeric"):
            numeric = stats["numeric"]
            lines.append(
                f"最小值: {numeric.get('min')} | 最大值: {numeric.get('max')} | 均值: {numeric.get('avg')}"
            )
        top = stats.get("top_values") or []
        lines.append("\n## 值分布 (Top 10)")
        if top:
            for entry in top:
                lines.append(f"- {entry['value']}: {entry['count']}")
        else:
            lines.append("（无法获取值分布）")
    else:
        lines.append("（数据库当前不可用，以上仅为抽样画像）")

    return ToolOutcome(
        ok=True,
        summary=f"{meta.name}.{column.name} 分析完成",
        content="\n".join(lines),
        payload={"table": meta.name, "column": column.name, "stats": stats or {}},
    )


async def _column_stats(
    ctx: ToolContext,
    meta: TableMeta,
    column: str,
    data_type: str,
    sample_values: list[str],
) -> dict[str, Any] | None:
    """向数据库要一组统计量。

    走 `executor.query`（因此受只读校验、语句超时与行数上限保护），
    任何一步失败都退化成"拿不到"，不让整个工具失败。
    """
    dialect = ctx.dialect
    quoted_table = dialect.quote(meta.name)
    quoted_column = dialect.quote(column)
    limit = dialect.limit_clause(10)
    is_numeric = data_type.lower() in (
        "int", "bigint", "smallint", "tinyint", "integer", "decimal",
        "numeric", "float", "double", "real", "number", "double precision",
    )

    stats: dict[str, Any] = {"total": meta.row_count}
    try:
        async with ctx.database() as executor:
            await _collect_stats(ctx, executor, stats, quoted_table, quoted_column, limit, is_numeric)
    except Exception as exc:  # noqa: BLE001 - 统计失败不该让工具失败
        logger.info("列统计 %s.%s 部分失败: %s: %s", meta.name, column, type(exc).__name__, exc)
        stats["partial"] = True
    return stats


async def _collect_stats(
    ctx: ToolContext,
    executor: Any,
    stats: dict[str, Any],
    quoted_table: str,
    quoted_column: str,
    limit: str,
    is_numeric: bool,
) -> None:
    """真正向数据库要统计量（受只读校验、语句超时与行数上限保护）。"""
    distinct_sql = (
        f"SELECT COUNT(DISTINCT {quoted_column}) AS distinct_count "
        f"FROM {quoted_table} WHERE {quoted_column} IS NOT NULL"
    )
    distinct = await executor.query(distinct_sql, snapshot=ctx.snapshot, scope=ctx.scope)
    if distinct.rows:
        stats["distinct"] = distinct.rows[0].get("distinct_count")

    null_sql = (
        f"SELECT COUNT(*) AS null_count FROM {quoted_table} "
        f"WHERE {quoted_column} IS NULL"
    )
    nulls = await executor.query(null_sql, snapshot=ctx.snapshot, scope=ctx.scope)
    if nulls.rows:
        stats["null_count"] = nulls.rows[0].get("null_count")

    if is_numeric:
        numeric_sql = (
            f"SELECT MIN({quoted_column}) AS mn, MAX({quoted_column}) AS mx, "
            f"AVG({quoted_column}) AS av FROM {quoted_table}"
        )
        numeric = await executor.query(numeric_sql, snapshot=ctx.snapshot, scope=ctx.scope)
        if numeric.rows:
            row = numeric.rows[0]
            stats["numeric"] = {
                "min": str(row.get("mn")),
                "max": str(row.get("mx")),
                "avg": _round_or_none(row.get("av")),
            }

    top_sql = (
        f"SELECT {quoted_column} AS val, COUNT(*) AS cnt FROM {quoted_table} "
        f"WHERE {quoted_column} IS NOT NULL "
        f"GROUP BY {quoted_column} ORDER BY cnt DESC {limit}"
    )
    top = await executor.query(top_sql, snapshot=ctx.snapshot, scope=ctx.scope)
    stats["top_values"] = [
        {"value": str(row.get("val"))[:30], "count": row.get("cnt")} for row in top.rows
    ]


def _round_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(round(float(value), 2))
    except (TypeError, ValueError):
        return str(value)


async def find_related_tables(params: FindRelatedParams, ctx: ToolContext) -> ToolOutcome:
    """单跳外键关联（含可直接使用的 JOIN 片段）。"""
    snapshot = _snapshot(ctx)
    if snapshot is None:
        return _missing_snapshot()

    tables = _scoped_tables(snapshot, ctx.scope)
    meta = _lookup(tables, params.table_name)
    if meta is None:
        return ToolOutcome.failure(f"表 {params.table_name} 不可用")

    outgoing, incoming = _related(snapshot, meta.name, ctx.scope)
    lines = [f"# 与 {meta.name} 关联的表\n"]
    if outgoing:
        lines.append(f"## outgoing（{meta.name} 引用其他表）")
        for fk in outgoing:
            lines.append(
                f"- {_quote(ctx, fk.table)} → {_quote(ctx, fk.ref_table)}: "
                f"JOIN {_quote(ctx, fk.ref_table)} ON {_quote(ctx, fk.table)}.{_quote(ctx, fk.column)} = "
                f"{_quote(ctx, fk.ref_table)}.{_quote(ctx, fk.ref_column)}"
            )
    if incoming:
        lines.append(f"\n## incoming（其他表引用 {meta.name}）")
        for fk in incoming:
            lines.append(
                f"- {_quote(ctx, fk.table)} → {_quote(ctx, fk.ref_table)}: "
                f"JOIN {_quote(ctx, fk.table)} ON {_quote(ctx, fk.table)}.{_quote(ctx, fk.column)} = "
                f"{_quote(ctx, fk.ref_table)}.{_quote(ctx, fk.ref_column)}"
            )
    if not outgoing and not incoming:
        lines.append("没有找到外键关系。")

    return ToolOutcome(
        ok=True,
        summary=f"{meta.name} 关联 {len(outgoing) + len(incoming)} 张表",
        content="\n".join(lines),
        payload={
            "table_name": meta.name,
            "outgoing": [fk.describe() for fk in outgoing],
            "incoming": [fk.describe() for fk in incoming],
        },
    )


def _related(snapshot: SchemaSnapshot, table: str, scope: CatalogScope) -> tuple[list, list]:
    """(outgoing, incoming) —— 出边是本表引用别人，入边是别人引用本表。"""
    outgoing = [
        fk
        for fk in snapshot.foreign_keys
        if fk.table.lower() == table.lower() and scope.allows(fk.ref_table)
    ]
    incoming = [
        fk
        for fk in snapshot.foreign_keys
        if fk.ref_table.lower() == table.lower() and scope.allows(fk.table)
    ]
    return outgoing, incoming


async def find_join_path(params: FindJoinPathParams, ctx: ToolContext) -> ToolOutcome:
    """两表之间的最短外键路径（BFS，多跳）。"""
    snapshot = _snapshot(ctx)
    if snapshot is None:
        return _missing_snapshot()

    tables = _scoped_tables(snapshot, ctx.scope)
    source = _lookup(tables, params.source_table)
    target = _lookup(tables, params.target_table)
    if source is None:
        return ToolOutcome.failure(f"表 {params.source_table} 不可用")
    if target is None:
        return ToolOutcome.failure(f"表 {params.target_table} 不可用")

    adjacency: dict[str, list[tuple[str, Any]]] = {}
    for fk in snapshot.foreign_keys:
        adjacency.setdefault(fk.table, []).append((fk.ref_table, fk))
        adjacency.setdefault(fk.ref_table, []).append((fk.table, fk))

    path = _bfs(adjacency, source.name, target.name)
    if path is None:
        return ToolOutcome(
            ok=True,
            summary="没有找到外键路径",
            content=(
                f"未找到 {source.name} 到 {target.name} 的外键路径 —— "
                f"它们之间没有直接或间接的外键关联。"
            ),
            payload={"hops": 0},
        )

    lines = [f"# {source.name} → {target.name} 的 JOIN 路径\n", f"共 {len(path)} 跳：\n"]
    for index, fk in enumerate(path, 1):
        lines.append(
            f"{index}. JOIN {_quote(ctx, fk.ref_table)} ON {_quote(ctx, fk.table)}.{_quote(ctx, fk.column)} = "
            f"{_quote(ctx, fk.ref_table)}.{_quote(ctx, fk.ref_column)}"
        )
    return ToolOutcome(
        ok=True,
        summary=f"{len(path)} 跳路径",
        content="\n".join(lines),
        payload={"hops": len(path), "path": [fk.describe() for fk in path]},
    )


def _bfs(adjacency: dict[str, list[tuple[str, Any]]], source: str, target: str) -> list[Any] | None:
    queue: deque[tuple[str, list[Any]]] = deque([(source, [])])
    visited = {source.lower()}
    while queue:
        current, path = queue.popleft()
        if current.lower() == target.lower():
            return path
        for neighbor, fk in adjacency.get(current, ()):
            if neighbor.lower() in visited:
                continue
            visited.add(neighbor.lower())
            queue.append((neighbor, [*path, fk]))
    return None


async def get_suggested_questions(params: EmptyParams, ctx: ToolContext) -> ToolOutcome:
    """推荐问题 —— 优先用探索报告里的 ```suggestions 块。"""
    snapshot = _snapshot(ctx)
    if snapshot is None:
        return _missing_snapshot()

    tables = _scoped_tables(snapshot, ctx.scope)
    suggestions = await _suggestions_from_report(ctx)
    if not suggestions:
        suggestions = [
            "介绍一下当前数据库架构",
            "数据库有哪些表？",
            f"查询 {next(iter(tables))} 表的数据" if tables else "查询数据",
        ]

    lines = ["# 推荐问题\n"]
    for index, suggestion in enumerate(suggestions[:5], 1):
        lines.append(f"{index}. {suggestion}")
    return ToolOutcome(
        ok=True,
        summary=f"{min(len(suggestions), 5)} 个推荐问题",
        content="\n".join(lines),
        payload={"suggestions": suggestions[:5]},
    )


async def _suggestions_from_report(ctx: ToolContext) -> list[str]:
    """从探索报告里抽推荐问题（报告由 `usecases.onboarding` 写入仓储）。

    走仓储的异步孪生（`areport`）—— SQLite 的磁盘 IO 不该出现在事件循环上。
    """
    try:
        from dbchat.storage.explorations import get_exploration_repository

        report = await get_exploration_repository().areport(ctx.profile_id)
    except Exception as exc:  # noqa: BLE001 - 没有报告不是错误
        logger.info("读取探索报告失败: %s: %s", type(exc).__name__, exc)
        return []
    return extract_suggestions(report)


def extract_suggestions(report: str) -> list[str]:
    """抽取报告末尾的 ```suggestions 块。"""
    if not report:
        return []
    match = re.search(r"```suggestions\s*\n(.*?)```", report, re.DOTALL)
    if not match:
        return []
    return [line.strip() for line in match.group(1).strip().splitlines() if line.strip()]
