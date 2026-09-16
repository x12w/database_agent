"""High-level skills — pre-computed, rich results that replace multiple low-level tool calls.

Each skill does significant work internally and returns structured data.
The LLM calls one skill instead of chaining 5-10 low-level tools.

Skills:
- get_schema_summary: Full database overview (tables, columns, relations, samples, distributions)
- get_table_detail: Complete info about one table
- analyze_column: Statistical analysis of a column
- find_related_tables: FK traversal with JOIN examples
- get_suggested_questions: Pre-generated question suggestions
"""

import json
import os
import time
from collections import Counter

from app.database.connection import db
from app.database.schema_cache import schema_manager, SchemaSnapshot, TableMeta


# ---- Skill 1: Schema Summary (the master skill) ----


def skill_schema_summary() -> dict:
    """Return a complete database overview — everything the LLM needs in one call.

    Includes: all tables with columns, row counts, sample data, foreign keys
    with pre-built JOIN examples, and value distributions.
    """
    snapshot = _get_snapshot()
    if not snapshot:
        return {"success": False, "content": "Schema 缓存不可用，请先连接数据库。"}

    lines = [f"# 数据库: {snapshot.db_name}", f"表数: {len(snapshot.tables)} | 外键: {len(snapshot.foreign_keys)}\n"]

    # Table overview
    lines.append("## 表概览")
    lines.append("| 表名 | 行数 | 列数 | 说明 |")
    lines.append("|------|------|------|------|")
    for tname, meta in snapshot.tables.items():
        lines.append(f"| {tname} | {meta.row_count} | {len(meta.columns)} | {meta.comment or '-'} |")
    lines.append("")

    # Table details with samples
    for tname, meta in snapshot.tables.items():
        lines.append(f"## 📋 {tname}（{meta.row_count} 行）")
        # Columns
        lines.append("| 字段 | 类型 | 可空 | 键 | 说明 |")
        lines.append("|------|------|------|----|------|")
        for col in meta.columns:
            lines.append(
                f"| {col.name} | {col.column_type} | "
                f"{'是' if col.nullable else '否'} | {col.key or '-'} | {col.comment or '-'} |"
            )
        # Sample data
        if meta.sample_rows:
            cols = list(meta.sample_rows[0].keys())[:6]
            lines.append(f"\n抽样 ({len(meta.sample_rows)} 行):")
            lines.append("| " + " | ".join(cols) + " |")
            lines.append("|" + "|".join(["---"] * len(cols)) + "|")
            for row in meta.sample_rows[:3]:
                vals = [str(row.get(c, "NULL"))[:25] if row.get(c) is not None else "NULL" for c in cols]
                lines.append("| " + " | ".join(vals) + " |")
        lines.append("")

    # Foreign keys with JOIN examples
    if snapshot.foreign_keys:
        lines.append("## 🔗 外键关系（含 JOIN 示例）")
        q = db.quote
        for fk in snapshot.foreign_keys:
            lines.append(
                f"- {q(fk.table_name)} JOIN {q(fk.referenced_table)} "
                f"ON {q(fk.table_name)}.{q(fk.column_name)} = "
                f"{q(fk.referenced_table)}.{q(fk.referenced_column)}"
            )
        lines.append("")

    # Value distributions
    if snapshot.summary and snapshot.summary.table_summaries:
        lines.append("## 📊 值分布")
        for tname, info in snapshot.summary.table_summaries.items():
            sv = info.get("sample_values", {})
            if sv:
                lines.append(f"**{tname}**:")
                for col, vals in sv.items():
                    if vals:
                        lines.append(f"  `{col}`: {', '.join(str(v)[:20] for v in vals[:6])}")
        lines.append("")

    return {
        "success": True,
        "content": "\n".join(lines),
        "raw_result": {
            "tables": list(snapshot.tables.keys()),
            "table_count": len(snapshot.tables),
            "foreign_key_count": len(snapshot.foreign_keys),
            "total_rows": snapshot.summary.total_rows_estimate if snapshot.summary else 0,
        },
    }


# ---- Skill 2: Table Detail ----


def skill_table_detail(table_name: str) -> dict:
    """Return complete information about one table including related tables."""
    snapshot = _get_snapshot()
    if not snapshot:
        return {"success": False, "content": "Schema 缓存不可用。"}

    # Lazy-load sample data for this table if needed
    from app.database.schema_cache import ensure_table_samples
    ensure_table_samples(table_name)

    meta = snapshot.tables.get(table_name)
    if not meta:
        return {"success": False, "content": f"表 {table_name} 不存在。可用表: {', '.join(snapshot.tables.keys())}"}

    lines = [f"# 表: {table_name}", f"行数: {meta.row_count} | 列数: {len(meta.columns)}\n"]

    # Columns
    lines.append("## 列")
    lines.append("| # | 字段 | 类型 | 可空 | 键 | 说明 |")
    lines.append("|---|------|------|------|----|------|")
    for i, col in enumerate(meta.columns, 1):
        lines.append(
            f"| {i} | {col.name} | {col.column_type} | "
            f"{'是' if col.nullable else '否'} | {col.key or '-'} | {col.comment or '-'} |"
        )

    # Sample data
    if meta.sample_rows:
        cols = list(meta.sample_rows[0].keys())
        lines.append(f"\n## 抽样数据 ({len(meta.sample_rows)} 行)")
        lines.append("| " + " | ".join(cols[:8]) + " |")
        lines.append("|" + "|".join(["---"] * min(8, len(cols))) + "|")
        for row in meta.sample_rows[:5]:
            vals = [str(row.get(c, "NULL"))[:20] if row.get(c) is not None else "NULL" for c in cols[:8]]
            lines.append("| " + " | ".join(vals) + " |")

    # Related tables via FK
    related = _find_related(snapshot, table_name)
    if related["incoming"] or related["outgoing"]:
        lines.append("\n## 关联表")
        q = db.quote
        for rel in related["outgoing"]:
            lines.append(f"- → {q(rel['table'])} ON {q(table_name)}.{q(rel['column'])} = {q(rel['table'])}.{q(rel['ref_column'])}")
        for rel in related["incoming"]:
            lines.append(f"- ← {q(rel['table'])} ON {q(rel['table'])}.{q(rel['column'])} = {q(table_name)}.{q(rel['ref_column'])}")

    # Value distribution
    if snapshot.summary:
        info = snapshot.summary.table_summaries.get(table_name, {})
        sv = info.get("sample_values", {})
        if sv:
            lines.append("\n## 值分布")
            for col, vals in sv.items():
                if vals:
                    lines.append(f"`{col}`: {', '.join(str(v)[:20] for v in vals[:5])}")

    return {
        "success": True,
        "content": "\n".join(lines),
        "raw_result": {
            "table_name": table_name,
            "row_count": meta.row_count,
            "column_count": len(meta.columns),
            "related_tables": [r["table"] for r in related["outgoing"] + related["incoming"]],
        },
    }


# ---- Skill 3: Column Analysis ----


def skill_analyze_column(table_name: str, column_name: str) -> dict:
    """Return statistical analysis of a specific column.

    Uses cached row counts for large tables and sets per-query timeouts
    to avoid expensive full scans on unindexed columns.
    """
    snapshot = _get_snapshot()
    meta = snapshot.tables.get(table_name) if snapshot else None
    if not meta:
        return {"success": False, "content": f"表 {table_name} 不存在。"}

    col_meta = next((c for c in meta.columns if c.name == column_name), None)
    if not col_meta:
        return {"success": False, "content": f"列 {column_name} 不存在于表 {table_name}。"}

    q = db.quote
    is_pg = (db.db_type == "postgresql")
    is_dm = (db.db_type in ("dameng", "dm", "dm8"))
    is_large_table = meta.row_count > 50000

    try:
        start = time.time()
        with db.cursor() as cursor:
            # Set statement timeout (3s) for large tables
            if is_large_table:
                if not is_pg and not is_dm:
                    try:
                        cursor.execute("SET SESSION MAX_EXECUTION_TIME = 3000")
                    except Exception:
                        pass
                elif is_pg:
                    try:
                        cursor.execute("SET LOCAL statement_timeout = '3s'")
                    except Exception:
                        pass

            # Use cached row count for large tables to avoid COUNT(*) full scan
            if is_large_table and meta.row_count > 0:
                total = meta.row_count
            else:
                try:
                    cursor.execute(f"SELECT COUNT(*) AS cnt FROM {q(table_name)}")
                    total = cursor.fetchone()["cnt"]
                except Exception:
                    total = meta.row_count or 0

            # COUNT DISTINCT (with timeout — may fail on large tables)
            distinct_count = 0
            try:
                cursor.execute(f"SELECT COUNT(DISTINCT {q(column_name)}) AS dc FROM {q(table_name)}")
                distinct_count = cursor.fetchone()["dc"]
            except Exception:
                distinct_count = -1  # Signal "unknown"

            # Numeric stats (MIN/MAX/AVG — usually fast with indexes)
            numeric_stats = {}
            dt = col_meta.data_type.lower() if col_meta.data_type else ""
            if dt in ("int", "bigint", "decimal", "float", "double", "tinyint", "smallint",
                      "integer", "numeric", "real", "double precision", "number"):
                try:
                    cursor.execute(
                        f"SELECT MIN({q(column_name)}) AS mn, MAX({q(column_name)}) AS mx, "
                        f"AVG({q(column_name)}) AS av FROM {q(table_name)}"
                    )
                    row = cursor.fetchone()
                    if row:
                        numeric_stats = {"min": str(row["mn"]), "max": str(row["mx"]),
                                        "avg": str(round(float(row["av"]), 2)) if row["av"] else None}
                except Exception:
                    pass

            # Top values — for large tables, use a limited scan to avoid expensive sorts
            top_values = []
            try:
                if is_large_table:
                    # For large tables: sample-based approach, limit rows scanned
                    if is_pg:
                        cursor.execute(
                            f"SELECT {q(column_name)} AS val, COUNT(*) AS cnt "
                            f"FROM (SELECT {q(column_name)} FROM {q(table_name)} "
                            f"TABLESAMPLE SYSTEM(10) WHERE {q(column_name)} IS NOT NULL) AS sampled "
                            f"GROUP BY {q(column_name)} ORDER BY cnt DESC LIMIT 10"
                        )
                    elif is_dm:
                        # Dameng: use ROWNUM to limit rows scanned before GROUP BY
                        cursor.execute(
                            f"SELECT val, COUNT(*) AS cnt FROM ("
                            f"SELECT {q(column_name)} AS val FROM {q(table_name)} "
                            f"WHERE {q(column_name)} IS NOT NULL AND ROWNUM <= 50000"
                            f") GROUP BY val ORDER BY cnt DESC LIMIT 10"
                        )
                    else:
                        # MySQL: use a subquery with LIMIT to restrict scan
                        cursor.execute(
                            f"SELECT val, COUNT(*) AS cnt FROM ("
                            f"SELECT {q(column_name)} AS val FROM {q(table_name)} "
                            f"WHERE {q(column_name)} IS NOT NULL LIMIT 50000"
                            f") AS sampled GROUP BY val ORDER BY cnt DESC LIMIT 10"
                        )
                else:
                    cursor.execute(
                        f"SELECT {q(column_name)} AS val, COUNT(*) AS cnt FROM {q(table_name)} "
                        f"GROUP BY {q(column_name)} ORDER BY cnt DESC LIMIT 10"
                    )
                top_values = [{"value": str(r["val"])[:30], "count": r["cnt"]} for r in cursor.fetchall()]
            except Exception:
                pass

            # Null count — combine with cached total for large tables
            null_count = 0
            try:
                if is_large_table:
                    # For large tables, estimate from sampling
                    cursor.execute(
                        f"SELECT SUM(CASE WHEN {q(column_name)} IS NULL THEN 1 ELSE 0 END) AS nc "
                        f"FROM (SELECT {q(column_name)} FROM {q(table_name)} LIMIT 50000) AS sampled"
                    )
                    sampled_nulls = cursor.fetchone()["nc"] or 0
                    # Rough projection (only report if sample had nulls)
                    null_count = sampled_nulls
                else:
                    cursor.execute(f"SELECT COUNT(*) AS nc FROM {q(table_name)} WHERE {q(column_name)} IS NULL")
                    null_count = cursor.fetchone()["nc"]
            except Exception:
                null_count = -1

        elapsed = round((time.time() - start) * 1000, 1)

        lines = [
            f"# 列分析: {table_name}.{column_name}",
            f"类型: {col_meta.column_type} | 键: {col_meta.key or '无'}",
        ]
        if total:
            distinct_str = str(distinct_count) if distinct_count >= 0 else "?"
            null_str = f"{null_count} ({round(null_count/total*100,1)}%)" if null_count >= 0 and total > 0 else "?"
            sampling_note = " (基于采样估算)" if is_large_table else ""
            lines.append(f"总行数: {total}{sampling_note} | 唯一值: {distinct_str} | NULL: {null_str}")
        if numeric_stats:
            lines.append(f"最小值: {numeric_stats['min']} | 最大值: {numeric_stats['max']} | 均值: {numeric_stats['avg']}")
        lines.append("\n## 值分布 (Top 10)")
        if top_values:
            for tv in top_values:
                if total and total > 0:
                    lines.append(f"- {tv['value']}: {tv['count']} ({round(tv['count']/total*100,1)}%)")
                else:
                    lines.append(f"- {tv['value']}: {tv['count']}")
        else:
            lines.append("（无法获取值分布）")
        if is_large_table:
            lines.append(f"\n⚠️ 大表模式：统计数据基于采样（前 50000 行），非全量精确值。执行时间: {elapsed}ms")

        return {
            "success": True,
            "content": "\n".join(lines),
            "raw_result": {
                "table": table_name, "column": column_name,
                "total": total, "distinct": distinct_count, "null_count": null_count,
                "top_values": top_values, "numeric_stats": numeric_stats,
                "execution_ms": elapsed, "sampling_used": is_large_table,
            },
        }
    except Exception as e:
        return {"success": False, "content": f"分析失败: {str(e)}"}


# ---- Skill 4: Find Related Tables ----


def skill_find_related(table_name: str) -> dict:
    """Find all tables related to the given table via foreign keys."""
    snapshot = _get_snapshot()
    if not snapshot:
        return {"success": False, "content": "Schema 缓存不可用。"}

    if table_name not in snapshot.tables:
        return {"success": False, "content": f"表 {table_name} 不存在。"}

    related = _find_related(snapshot, table_name)

    lines = [f"# 与 {table_name} 关联的表\n"]

    q = db.quote
    if related["outgoing"]:
        lines.append(f"##  outgoing（{table_name} 引用其他表）")
        for rel in related["outgoing"]:
            lines.append(
                f"- {q(table_name)} → {q(rel['table'])}: "
                f"JOIN {q(rel['table'])} ON {q(table_name)}.{q(rel['column'])} = {q(rel['table'])}.{q(rel['ref_column'])}"
            )

    if related["incoming"]:
        lines.append(f"\n##  incoming（其他表引用 {table_name}）")
        for rel in related["incoming"]:
            lines.append(
                f"- {q(rel['table'])} → {q(table_name)}: "
                f"JOIN {q(table_name)} ON {q(rel['table'])}.{q(rel['column'])} = {q(table_name)}.{q(rel['ref_column'])}"
            )

    if not related["outgoing"] and not related["incoming"]:
        lines.append("没有找到外键关系。")

    return {
        "success": True,
        "content": "\n".join(lines),
        "raw_result": related,
    }


# ---- Skill 5: Suggested Questions ----


def skill_suggested_questions() -> dict:
    """Return pre-generated question suggestions from exploration report."""
    snapshot = _get_snapshot()
    if not snapshot:
        return {"success": False, "content": "Schema 缓存不可用。"}

    # Try to load from exploration
    suggestions = None
    try:
        import re
        for fn in os.listdir("data/explorations") if os.path.isdir("data/explorations") else []:
            if fn.endswith(".json"):
                with open(os.path.join("data/explorations", fn)) as f:
                    d = json.load(f)
                if d.get("db_name") == snapshot.db_name:
                    m = re.search(r'```suggestions\s*\n(.*?)```', d.get("report", ""), re.DOTALL)
                    if m:
                        suggestions = [s.strip() for s in m.group(1).strip().split('\n') if s.strip()]
                    break
    except Exception:
        pass

    if not suggestions:
        suggestions = [
            "介绍一下当前数据库架构",
            f"数据库有哪些表？",
            f"查询 {list(snapshot.tables.keys())[0]} 表的数据" if snapshot.tables else "查询数据",
        ]

    lines = ["# 推荐问题\n"]
    for i, s in enumerate(suggestions[:5], 1):
        lines.append(f"{i}. {s}")

    return {
        "success": True,
        "content": "\n".join(lines),
        "raw_result": {"suggestions": suggestions[:5]},
    }


# ---- Helpers ----


def _get_snapshot() -> SchemaSnapshot:
    """Get the current schema snapshot. Uses TTL + checksum freshness.

    Only force-rebuilds if the snapshot is truly missing (None).
    Otherwise relies on the cache's TTL (5 min) and checksum check.
    """
    if not db.current_profile:
        return None
    pid = db.current_profile.id
    cache = schema_manager.get_cache(pid)
    # Only force rebuild if we have no snapshot at all
    if cache.snapshot is None:
        schema_manager.invalidate(pid)
    snap = schema_manager.ensure_fresh(pid)
    if snap is None:
        # Last resort: build directly
        try:
            from app.database.schema_cache import build_schema_snapshot
            snap = build_schema_snapshot(db.current_db_name)
        except Exception as e:
            print(f"[Skills] Failed to build snapshot: {e}")
    return snap


def _find_related(snapshot: SchemaSnapshot, table_name: str) -> dict:
    """Find incoming and outgoing FK relationships for a table."""
    outgoing = []
    incoming = []
    for fk in snapshot.foreign_keys:
        if fk.table_name == table_name:
            outgoing.append({
                "table": fk.referenced_table,
                "column": fk.column_name,
                "ref_column": fk.referenced_column,
            })
        elif fk.referenced_table == table_name:
            incoming.append({
                "table": fk.table_name,
                "column": fk.column_name,
                "ref_column": fk.referenced_column,
            })
    return {"outgoing": outgoing, "incoming": incoming}


def skill_find_join_path(source_table: str, target_table: str) -> dict:
    """Find the shortest FK path between two tables (multi-hop traversal).

    Uses BFS to find the shortest chain of JOINs connecting source → target.
    Returns pre-built JOIN SQL for each hop, or indicates no path found.
    """
    snapshot = _get_snapshot()
    if not snapshot or not snapshot.foreign_keys:
        return {"success": False, "content": "Schema 缓存不可用。"}

    if source_table not in snapshot.tables:
        return {"success": False, "content": f"表 {source_table} 不存在。"}
    if target_table not in snapshot.tables:
        return {"success": False, "content": f"表 {target_table} 不存在。"}

    # Build adjacency: table → [(fk, direction)]
    # direction: "out" means table has FK pointing to another; "in" means another points to table
    from collections import deque
    adj = {}
    for fk in snapshot.foreign_keys:
        adj.setdefault(fk.table_name, []).append((fk.referenced_table, fk, "out"))
        adj.setdefault(fk.referenced_table, []).append((fk.table_name, fk, "in"))

    # BFS
    q = deque()
    q.append((source_table, []))  # (current_table, path_of_fks)
    visited = {source_table}

    while q:
        current, path = q.popleft()
        if current == target_table:
            # Build the JOIN chain
            lines = [f"# {source_table} → {target_table} 的 JOIN 路径\n",
                     f"共 {len(path)} 跳：\n"]
            q_char = db.quote
            for i, (fk, direction) in enumerate(path):
                if direction == "out":
                    lines.append(
                        f"{i+1}. JOIN {q_char(fk.referenced_table)} ON "
                        f"{q_char(fk.table_name)}.{q_char(fk.column_name)} = "
                        f"{q_char(fk.referenced_table)}.{q_char(fk.referenced_column)}"
                    )
                else:
                    lines.append(
                        f"{i+1}. JOIN {q_char(fk.table_name)} ON "
                        f"{q_char(fk.table_name)}.{q_char(fk.column_name)} = "
                        f"{q_char(fk.referenced_table)}.{q_char(fk.referenced_column)}"
                    )
            full_sql_lines = []
            for i, (fk, direction) in enumerate(path):
                if direction == "out":
                    full_sql_lines.append(
                        f"JOIN {q_char(fk.referenced_table)} ON "
                        f"{q_char(fk.table_name)}.{q_char(fk.column_name)} = "
                        f"{q_char(fk.referenced_table)}.{q_char(fk.referenced_column)}"
                    )
                else:
                    full_sql_lines.append(
                        f"JOIN {q_char(fk.table_name)} ON "
                        f"{q_char(fk.table_name)}.{q_char(fk.column_name)} = "
                        f"{q_char(fk.referenced_table)}.{q_char(fk.referenced_column)}"
                    )
            lines.append(f"\n完整 JOIN 链：\n{' '.join(full_sql_lines)}")
            return {"success": True, "content": "\n".join(lines),
                    "raw_result": {"path": path, "hops": len(path)}}

        for neighbor, fk, direction in adj.get(current, []):
            if neighbor not in visited:
                visited.add(neighbor)
                q.append((neighbor, path + [(fk, direction)]))

    # No path found
    return {"success": True,
            "content": f"未找到 {source_table} 到 {target_table} 的 FK 路径。"
                       f"它们之间没有直接或间接的外键关联。"}
