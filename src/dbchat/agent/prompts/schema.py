"""结构上下文渲染器 —— 提示词里最大的一块。

从 `app/agent/prompt_builder.py:_build_schema_context`（:24-219）移植，
三处硬编码被换成方言/配置：

* `db.quote` → `Dialect.quote()`（历史实现从全局连接对象上取引用字符）；
* `COMPACT_SCHEMA_THRESHOLD = 20` → `Settings.large_db_table_threshold`
  （同一个阈值散在 orchestrator、prompt_builder、background_explorer 三处）；
* 列画像来自 `ColumnMeta.value_type`（内省阶段已算好），不再依赖已删除的
  `snapshot.summary.column_profiles`。

大库压缩策略与两跳 JOIN 示例（`_append_transitive_joins`）的行为刻意保留：
它们是大库上"上下文不爆但计划仍可用"的关键。
"""

from __future__ import annotations

import logging

from dbchat.config import Settings
from dbchat.db.dialects.base import Dialect
from dbchat.domain.schema import (
    CatalogScope,
    ColumnValueType,
    ForeignKeyMeta,
    SchemaSnapshot,
    TableMeta,
)

logger = logging.getLogger(__name__)

__all__ = ["build_schema_context", "extract_table_refs"]

#: 单张表最多展示的列数（非重点表）。
_COLUMN_CAP = 12
#: 大库模式下最多给多少张表展开详情。
_DETAIL_TABLE_CAP = 10
#: 大库模式下最多展示的外键条数。
_FK_CAP_LARGE = 30

#: 列画像 → 提示词里的短提示。
_PROFILE_HINTS: dict[ColumnValueType, str] = {
    ColumnValueType.ALL_NULL: "⚠️NULL",
    ColumnValueType.CODED: "🔤编码",
    ColumnValueType.CHINESE: "📝中文",
    ColumnValueType.NUMERIC: "🔢数值",
    ColumnValueType.BOOLEAN_LIKE: "☑️布尔",
    ColumnValueType.DATETIME: "🕐时间",
}


def extract_table_refs(text: str) -> set[str]:
    """从计划/问题里提取被引用的表名（大库模式下据此决定展开哪些表）。"""
    import re

    refs: set[str] = set()
    for match in re.finditer(r'["`]([A-Za-z_][A-Za-z0-9_]*)["`]', text):
        refs.add(match.group(1))
    for match in re.finditer(
        r'(?:FROM|JOIN|表\s*[：:]?\s*)\s*["`]?([A-Za-z_][A-Za-z0-9_]*)',
        text,
        re.IGNORECASE,
    ):
        refs.add(match.group(1))
    return refs


async def build_schema_context(
    snapshot: SchemaSnapshot | None,
    *,
    scope: CatalogScope | None = None,
    dialect: Dialect | None = None,
    focus_tables: set[str] | None = None,
    profile_id: str = "",
    settings: Settings,
) -> str:
    """渲染结构上下文。

    Args:
        snapshot: 结构快照。
        scope: 表筛选范围；只展示范围内的表。
        dialect: 当前方言（决定标识符引用字符）。
        focus_tables: 需要展开详情的表（来自分析计划）。
        profile_id: 用于读取探索进度与管理员说明。
        settings: 大库阈值等配置来源。
    """
    if snapshot is None:
        return ""

    scope = scope or CatalogScope()
    tables = _scoped(snapshot, scope)
    foreign_keys = [
        fk for fk in snapshot.foreign_keys if fk.table in tables and scope.allows(fk.ref_table)
    ]
    n_tables = len(tables)
    is_large = n_tables > settings.large_db_table_threshold

    lines = [f"## 数据库: {snapshot.db_name}"]
    if scope.tables is not None:
        lines.append(
            f"⚠️ 表筛选模式 — 仅显示 {n_tables} 张选定的表（共 {snapshot.table_count} 张可用）"
        )
    lines.append(f"表数: {n_tables} | 外键关系: {len(foreign_keys)}")
    if snapshot.stale:
        lines.append("⚠️ 该结构来自缓存，可能落后于实际库结构")

    progress_note = await _progress_note(tables, profile_id)
    if progress_note:
        lines.append(progress_note)

    if is_large:
        lines.append("⚠️ 大数据库模式 — 仅展示重点表详情，其余仅索引\n")

    fk_connections = _fk_connection_counts(foreign_keys)

    lines.extend(_table_index(tables, fk_connections))
    lines.extend(
        _table_details(
            tables,
            foreign_keys,
            fk_connections,
            is_large=is_large,
            focus_tables=focus_tables,
        )
    )

    if foreign_keys:
        lines.extend(_foreign_key_section(foreign_keys, tables, dialect, is_large))

    if not is_large:
        lines.extend(_distribution_section(tables))

    lines.extend(await _clarification_section(tables, profile_id))
    return "\n".join(lines)


def _scoped(snapshot: SchemaSnapshot, scope: CatalogScope) -> dict[str, TableMeta]:
    if scope.tables is None:
        return dict(snapshot.tables)
    return {name: meta for name, meta in snapshot.tables.items() if scope.allows(name)}


async def _progress_note(tables: dict[str, TableMeta], profile_id: str) -> str:
    """探索进度 —— 让模型知道"这个库已经理解到什么程度"。"""
    if not profile_id:
        return ""
    try:
        from dbchat.storage.clarifications import get_clarification_repository

        progress = await get_clarification_repository().aget_exploration_progress(
            profile_id, {name: len(meta.columns) for name, meta in tables.items()}
        )
    except Exception as exc:  # noqa: BLE001 - 进度只是锦上添花，不能拖垮提示词
        logger.info("读取探索进度失败: %s: %s", type(exc).__name__, exc)
        return ""

    parts: list[str] = []
    if progress["clarified_columns"]:
        parts.append(f"💬{progress['clarified_columns']}列已知")
    if progress["pending_columns"]:
        parts.append(f"❓{progress['pending_columns']}列待确认")
    parts.append(f"🔍{progress['unknown_columns']}列待探索")
    total = sum(len(meta.columns) for meta in tables.values())
    return f"探索进度: {' | '.join(parts)} (共{total}列)"


async def _clarification_section(tables: dict[str, TableMeta], profile_id: str) -> list[str]:
    """管理员已答说明与待确认列。

    两者都直接影响生成 SQL 的正确性：已知列含义必须被采用，待确认列不该
    被反复提问。
    """
    if not profile_id:
        return []
    try:
        from dbchat.storage.clarifications import get_clarification_repository

        repository = get_clarification_repository()
        answered = await repository.aget_all_answered(profile_id)
        pending = await repository.aget_pending(profile_id)
    except Exception as exc:  # noqa: BLE001 - 读不到就当没有，但必须留痕
        logger.info("读取管理员说明失败: %s: %s", type(exc).__name__, exc)
        return []

    lines: list[str] = []
    known = [entry for entry in answered if entry.table_name in tables]
    if known:
        lines.append("### 💬 已知列含义（管理员说明）")
        lines.append("以下说明来自数据库管理员，是列含义的权威定义：")
        by_table: dict[str, list] = {}
        for entry in known:
            by_table.setdefault(entry.table_name, []).append(entry)
        for table_name, entries in list(by_table.items())[:10]:
            for entry in entries[:3]:
                if entry.column_name:
                    lines.append(f"- `{table_name}`.`{entry.column_name}`: {entry.answer[:200]}")
                else:
                    lines.append(f"- `{table_name}`: {entry.answer[:200]}")
        lines.append("")

    waiting = [entry for entry in pending if entry.table_name in tables]
    if waiting:
        lines.append("### ❓ 待确认列（已向管理员提问，等待回答）")
        lines.append("以下列已提交问题但尚未收到回答，无需重复提问：")
        by_table = {}
        for entry in waiting:
            by_table.setdefault(entry.table_name, []).append(entry)
        for table_name, entries in by_table.items():
            columns = [
                f"`{e.column_name}`" if e.column_name else "（整表）" for e in entries
            ]
            lines.append(f"- `{table_name}`: {', '.join(columns)}")
        lines.append("")
    return lines


def _fk_connection_counts(foreign_keys: list[ForeignKeyMeta]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for fk in foreign_keys:
        counts[fk.table] = counts.get(fk.table, 0) + 1
        counts[fk.ref_table] = counts.get(fk.ref_table, 0) + 1
    return counts


def _table_index(tables: dict[str, TableMeta], fk_connections: dict[str, int]) -> list[str]:
    lines = [
        "### 📑 表索引",
        "| 表名 | 行数 | 列数 | 关联表 |",
        "|------|------|------|--------|",
    ]
    for name, meta in tables.items():
        count = fk_connections.get(name, 0)
        lines.append(
            f"| {name} | {meta.row_count if meta.row_count is not None else '?'} | "
            f"{len(meta.columns)} | {'🔗×' + str(count) if count else '-'} |"
        )
    lines.append("")
    return lines


def _table_details(
    tables: dict[str, TableMeta],
    foreign_keys: list[ForeignKeyMeta],
    fk_connections: dict[str, int],
    *,
    is_large: bool,
    focus_tables: set[str] | None,
) -> list[str]:
    lines: list[str] = []
    if is_large:
        detail_tables: set[str] = set(focus_tables or ()) & set(tables)
        for name, meta in tables.items():
            if fk_connections.get(name, 0) >= 2 and meta.row_count:
                detail_tables.add(name)
        detail_tables = set(sorted(detail_tables)[:_DETAIL_TABLE_CAP])
        if detail_tables:
            lines.append(f"### 📋 重点表详情（共 {len(detail_tables)} 张）")
            lines.append("")
            for name in sorted(detail_tables):
                is_focus = bool(focus_tables and name in focus_tables)
                lines.extend(_table_detail(name, tables[name], is_focus=is_focus))
        return lines

    lines.append("### 📋 全部表详情")
    lines.append("")
    for name, meta in tables.items():
        lines.extend(_table_detail(name, meta))
    return lines


def _table_detail(name: str, meta: TableMeta, *, is_focus: bool = False) -> list[str]:
    lines: list[str] = []
    comment = f" — {meta.comment}" if meta.comment else ""
    column_cap = _COLUMN_CAP if not is_focus else max(_COLUMN_CAP, len(meta.columns))
    lines.append(
        f"#### {name}{comment}（{meta.row_count if meta.row_count is not None else '?'} 行, {len(meta.columns)} 列）"
    )

    parts: list[str] = []
    for column in meta.columns:
        key_mark = "🔑" if column.is_primary_key else ""
        hint = _PROFILE_HINTS.get(column.value_type) if column.value_type else ""
        if hint:
            parts.append(f"{key_mark}`{column.name}`({hint})")
        else:
            parts.append(f"{key_mark}`{column.name}`({column.column_type or column.data_type})")
    lines.append(", ".join(parts[:column_cap]))
    if len(parts) > column_cap:
        lines.append(f"  ...及其他 {len(parts) - column_cap} 列")

    null_columns = [
        column.name for column in meta.columns if column.value_type is ColumnValueType.ALL_NULL
    ]
    if null_columns:
        suffix = f" 等{len(null_columns)}列" if len(null_columns) > 5 else ""
        lines.append(f"⚠️ 全 NULL 列: {', '.join(null_columns[:5])}{suffix}")

    index_text = _index_text(meta)
    if index_text:
        lines.append(f"📌 索引: {index_text}")

    if meta.sample_rows:
        sample_columns = list(meta.sample_rows[0].keys())[:5]
        lines.append(f"\n抽样 ({len(meta.sample_rows)} 行):")
        lines.append("| " + " | ".join(sample_columns) + " |")
        lines.append("|" + "|".join(["---"] * len(sample_columns)) + "|")
        for row in meta.sample_rows[:3]:
            values = [
                "NULL" if row.get(column) is None else str(row.get(column))[:25]
                for column in sample_columns
            ]
            lines.append("| " + " | ".join(values) + " |")
    lines.append("")
    return lines


def _index_text(meta: TableMeta) -> str:
    if not meta.indexes:
        return ""
    parts: list[str] = []
    for index in meta.indexes:
        columns = ", ".join(index.columns) or index.name
        if index.unique:
            parts.append(f"⚡`{columns}`(唯一)")
        else:
            parts.append(f"`{columns}`")
    return ", ".join(parts[:8])


def _foreign_key_section(
    foreign_keys: list[ForeignKeyMeta],
    tables: dict[str, TableMeta],
    dialect: Dialect | None,
    is_large: bool,
) -> list[str]:
    lines = [
        "### 🔗 外键关系（JOIN 参考）",
        "**JOIN 规则**: FK 列可空(Nullable) → 用 LEFT JOIN；FK 列非空 → 用 INNER JOIN",
        "**方向**: `子表` → `父表` 表示子表的 FK 引用父表的主键",
        "",
    ]
    cap = _FK_CAP_LARGE if is_large else len(foreign_keys)
    for fk in foreign_keys[:cap]:
        child = tables.get(fk.table)
        nullable = False
        if child is not None:
            column = child.column(fk.column)
            nullable = bool(column and column.nullable)
        join_type = "LEFT JOIN" if nullable else "JOIN"
        hint = " ⚠️FK可空→LEFT" if nullable else ""
        parent_table = _q(dialect, fk.ref_table)
        child_table = _q(dialect, fk.table)
        lines.append(
            f"- {child_table}.{_q(dialect, fk.column)} → "
            f"{parent_table}.{_q(dialect, fk.ref_column)}{hint}\n"
            f"  `{join_type} {parent_table} ON "
            f"{child_table}.{_q(dialect, fk.column)} = {parent_table}.{_q(dialect, fk.ref_column)}`"
        )
    if is_large and len(foreign_keys) > cap:
        lines.append(f"- ...及其他 {len(foreign_keys) - cap} 个外键")

    lines.extend(_transitive_joins(foreign_keys, dialect))
    lines.append("")
    return lines


def _transitive_joins(
    foreign_keys: list[ForeignKeyMeta], dialect: Dialect | None
) -> list[str]:
    """两跳 JOIN 示例 —— 三表以上的常见查询模式。"""
    by_child: dict[str, list[ForeignKeyMeta]] = {}
    for fk in foreign_keys:
        by_child.setdefault(fk.table, []).append(fk)

    paths: list[str] = []
    for child, children in list(by_child.items())[:10]:
        for fk in children[:3]:
            parent = fk.ref_table
            if parent not in by_child or parent == child:
                continue
            for second in by_child[parent][:2]:
                grandparent = second.ref_table
                if grandparent in (child, parent):
                    continue
                paths.append(
                    f"- `{_q(dialect, child)}` → `{_q(dialect, parent)}` → `{_q(dialect, grandparent)}`\n"
                    f"  `JOIN {_q(dialect, parent)} ON {_q(dialect, child)}.{_q(dialect, fk.column)} = "
                    f"{_q(dialect, parent)}.{_q(dialect, fk.ref_column)}`\n"
                    f"  `JOIN {_q(dialect, grandparent)} ON {_q(dialect, parent)}.{_q(dialect, second.column)} = "
                    f"{_q(dialect, grandparent)}.{_q(dialect, second.ref_column)}`"
                )
                if len(paths) >= 5:
                    break
            if len(paths) >= 5:
                break
        if len(paths) >= 5:
            break

    if not paths:
        return []
    return ["", "**🔗 多表联查示例（2跳路径）**：", *paths]


def _distribution_section(tables: dict[str, TableMeta]) -> list[str]:
    lines: list[str] = []
    for name, meta in tables.items():
        entries = [
            f"  - `{column.name}`: {', '.join(str(v)[:20] for v in column.sample_values[:5])}"
            for column in meta.columns
            if column.sample_values
        ]
        if entries:
            if not lines:
                lines.append("### 📊 数据分布")
            lines.append(f"**{name}**:")
            lines.extend(entries)
    if lines:
        lines.append("")
    return lines


def _q(dialect: Dialect | None, ident: str) -> str:
    return dialect.quote(ident) if dialect is not None else ident
