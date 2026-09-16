"""Build system prompts for the agent — pre-computed, minimal LLM dependency."""

from app.database.connection import db
from app.database.schema_cache import SchemaSnapshot


# Threshold for compact schema mode (avoid context overflow)
COMPACT_SCHEMA_THRESHOLD = 20


def _extract_table_refs(text: str) -> set:
    """Extract table names referenced in an analysis plan or question."""
    import re
    refs = set()
    # Match quoted identifiers: "table_name" or `table_name`
    for m in re.finditer(r'["`]([A-Za-z_][A-Za-z0-9_]*)["`]', text):
        refs.add(m.group(1))
    # Match unquoted identifiers after keywords
    for m in re.finditer(r'(?:FROM|JOIN|表\s*[：:]?\s*)\s*["`]?([A-Za-z_][A-Za-z0-9_]*)', text, re.IGNORECASE):
        refs.add(m.group(1))
    return refs


def _build_schema_context(snapshot: SchemaSnapshot = None, focus_tables: set = None, table_filter: list = None) -> str:
    """Build schema context — adaptive based on database size.

    Small DB (≤20 tables): full detail with columns, samples, distributions.
    Large DB (>20 tables): compact table index + FK reference, details only
    for focus_tables (from analysis plan) or top-10 priority tables.

    Args:
        snapshot: The schema snapshot.
        focus_tables: Optional set of table names to show in detail (from analysis plan).
        table_filter: Optional list of allowed table names — only these are shown.
    """
    if snapshot is None:
        return ""

    # Apply table filter if set — only show allowed tables
    allowed_tables = set(table_filter) if table_filter else None
    tables = snapshot.tables
    foreign_keys = snapshot.foreign_keys
    if allowed_tables is not None:
        tables = {k: v for k, v in tables.items() if k in allowed_tables}
        foreign_keys = [fk for fk in foreign_keys
                       if fk.table_name in allowed_tables and fk.referenced_table in allowed_tables]

    n_tables = len(tables)
    is_large = n_tables > COMPACT_SCHEMA_THRESHOLD
    q = db.quote

    lines = [f"## 数据库: {snapshot.db_name or db.current_db_name}"]
    if allowed_tables is not None:
        lines.append(f"⚠️ 表筛选模式 — 仅显示 {n_tables} 张选定的表（共 {len(snapshot.tables)} 张可用）")
    lines.append(f"表数: {n_tables} | 外键关系: {len(foreign_keys)}")

    # Exploration progress summary — shows how much of the database is understood
    try:
        from app.rag.admin_clarifications import clarification_store
        from app.database.connection import db as _db
        _pid = _db.current_profile.id if _db.current_profile else "default"
        total_cols = sum(len(meta.columns) for meta in tables.values())
        progress = clarification_store.get_exploration_progress(
            _pid,
            {tname: len(meta.columns) for tname, meta in tables.items()}
        )
        if progress["clarified_columns"] > 0 or progress["pending_columns"] > 0:
            parts_summary = []
            if progress["clarified_columns"] > 0:
                parts_summary.append(f"💬{progress['clarified_columns']}列已知")
            if progress["pending_columns"] > 0:
                parts_summary.append(f"❓{progress['pending_columns']}列待确认")
            parts_summary.append(f"🔍{progress['unknown_columns']}列待探索")
            lines.append(f"探索进度: {' | '.join(parts_summary)} (共{total_cols}列)")
    except Exception:
        pass

    # Get column profiles from summary (available in both modes)
    all_profiles = {}
    if snapshot.summary and hasattr(snapshot.summary, 'column_profiles') and snapshot.summary.column_profiles:
        all_profiles = snapshot.summary.column_profiles

    if is_large:
        lines.append(f"⚠️ 大数据库模式 — 仅展示重点表详情，其余仅索引\n")

    # ---- Compact table index (always included) ----
    lines.append("### 📑 表索引")
    lines.append("| 表名 | 行数 | 列数 | 关联表 |")
    lines.append("|------|------|------|--------|")
    # Count FK connections per table
    fk_connections = {}
    for fk in foreign_keys:
        fk_connections[fk.table_name] = fk_connections.get(fk.table_name, 0) + 1
        fk_connections[fk.referenced_table] = fk_connections.get(fk.referenced_table, 0) + 1

    for tname, meta in tables.items():
        fk_count = fk_connections.get(tname, 0)
        fk_str = f"🔗×{fk_count}" if fk_count > 0 else "-"
        lines.append(f"| {tname} | {meta.row_count or '?'} | {len(meta.columns)} | {fk_str} |")
    lines.append("")

    # ---- Detailed view ----
    if is_large:
        # Determine which tables to show in detail
        if focus_tables:
            detail_tables = focus_tables & set(tables.keys())
        else:
            detail_tables = set()
        # Always include tables with FK connections and data
        for tname, meta in tables.items():
            if fk_connections.get(tname, 0) >= 2 and meta.row_count and meta.row_count > 0:
                detail_tables.add(tname)
        # Cap at 10 tables to avoid context overflow
        detail_tables = set(list(detail_tables)[:10])

        if detail_tables:
            lines.append(f"### 📋 重点表详情（共 {len(detail_tables)} 张）")
            lines.append("")
            for tname in sorted(detail_tables):
                is_focus = bool(focus_tables and tname in focus_tables)
                _append_table_detail(lines, tname, tables[tname], is_large, is_focus,
                                     all_profiles.get(tname, {}))
    else:
        # Small DB: show all tables in detail
        lines.append(f"### 📋 全部表详情")
        lines.append("")
        for tname, meta in tables.items():
            _append_table_detail(lines, tname, meta, col_profiles=all_profiles.get(tname, {}))

    # ---- Foreign Keys with JOIN guidance ----
    if foreign_keys:
        lines.append("### 🔗 外键关系（JOIN 参考）")
        lines.append("**JOIN 规则**: FK 列可空(Nullable) → 用 LEFT JOIN；FK 列非空 → 用 INNER JOIN")
        lines.append("**方向**: `子表` → `父表` 表示子表的 FK 引用父表的主键")
        lines.append("")
        # Group by FK holder (child table) for readability
        max_fks = 30 if is_large else len(foreign_keys)
        for fk in foreign_keys[:max_fks]:
            # Check if FK column is nullable → LEFT JOIN needed
            child_meta = tables.get(fk.table_name)
            nullable = False
            if child_meta:
                for col in child_meta.columns:
                    if col.name == fk.column_name:
                        nullable = col.nullable
                        break
            join_type = "LEFT JOIN" if nullable else "JOIN"
            nullable_hint = " ⚠️FK可空→LEFT" if nullable else ""
            lines.append(
                f"- {q(fk.table_name)}.{q(fk.column_name)} → "
                f"{q(fk.referenced_table)}.{q(fk.referenced_column)}"
                f"{nullable_hint}\n"
                f"  `{join_type} {q(fk.referenced_table)} ON "
                f"{q(fk.table_name)}.{q(fk.column_name)} = "
                f"{q(fk.referenced_table)}.{q(fk.referenced_column)}`"
            )
        if is_large and len(foreign_keys) > max_fks:
            lines.append(f"- ...及其他 {len(foreign_keys) - max_fks} 个外键")

        # Multi-hop transitive JOIN example (for common patterns)
        _append_transitive_joins(lines, foreign_keys, tables, q, is_large)
        lines.append("")

    # ---- Value distributions (only for small DBs) ----
    if not is_large and snapshot.summary and snapshot.summary.table_summaries:
        lines.append("### 📊 数据分布")
        for tname, info in snapshot.summary.table_summaries.items():
            sample_vals = info.get("sample_values", {})
            if sample_vals:
                lines.append(f"**{tname}**:")
                for col, vals in sample_vals.items():
                    if vals:
                        lines.append(f"  - `{col}`: {', '.join(str(v)[:20] for v in vals[:5])}")
        lines.append("")

    # Inject admin clarifications for known columns and pending questions
    try:
        from app.rag.admin_clarifications import clarification_store
        from app.database.connection import db as _db
        _pid = _db.current_profile.id if _db.current_profile else "default"
        all_answered = clarification_store.get_all_answered(_pid)
        all_pending = clarification_store.get_pending(_pid)

        if all_answered:
            # Filter to tables in current schema scope
            by_table: dict[str, list] = {}
            for entry in all_answered:
                if entry.table_name in tables:
                    by_table.setdefault(entry.table_name, []).append(entry)

            if by_table:
                lines.append("### 💬 已知列含义（管理员说明）")
                lines.append("以下说明来自数据库管理员，是列含义的权威定义：")
                for tname, entries in list(by_table.items())[:10]:
                    for entry in entries[:3]:
                        if entry.column_name:
                            lines.append(f"- `{tname}`.`{entry.column_name}`: {entry.answer[:200]}")
                        else:
                            lines.append(f"- `{tname}`: {entry.answer[:200]}")
                lines.append("")

        if all_pending:
            # Show pending questions grouped by table
            pending_by_table: dict[str, list] = {}
            for entry in all_pending:
                if entry.table_name in tables:
                    pending_by_table.setdefault(entry.table_name, []).append(entry)

            if pending_by_table:
                lines.append("### ❓ 待确认列（已向管理员提问，等待回答）")
                lines.append("以下列已提交问题但尚未收到回答，无需重复提问：")
                for tname, entries in pending_by_table.items():
                    cols = [f"`{e.column_name}`" if e.column_name else "（整表）" for e in entries]
                    lines.append(f"- `{tname}`: {', '.join(cols)}")
                lines.append("")
    except Exception:
        pass

    return "\n".join(lines)


def _append_table_detail(lines: list, table_name: str, meta, is_large: bool = False, is_focus: bool = False, col_profiles: dict = None):
    """Append detailed view of a single table to lines.

    Args:
        is_focus: If True (table is in analysis plan focus), show ALL columns.
        col_profiles: Optional dict of column_name → {value_type, null_ratio, sample_vals}.
    """
    if col_profiles is None:
        col_profiles = {}
    comment_str = f" — {meta.comment}" if meta.comment else ""
    column_cap = len(meta.columns) + 1 if is_focus else 12
    lines.append(f"#### {table_name}{comment_str}（{meta.row_count} 行, {len(meta.columns)} 列）")

    # Columns (compact format, with semantic profiles)
    col_parts = []
    for col in meta.columns:
        key_mark = "🔑" if col.key else ""
        profile = col_profiles.get(col.name, {})
        vtype = profile.get('value_type', '')
        null_r = profile.get('null_ratio', 0)
        # Add semantic hints
        hint = ""
        if vtype == "全NULL ⚠️":
            hint = "⚠️NULL"
        elif vtype == "编码/缩写":
            hint = "🔤编码"
        elif vtype == "中文文本":
            hint = "📝中文"
        elif vtype == "数值":
            hint = "🔢数值"
        if hint:
            col_parts.append(f"{key_mark}`{col.name}`({hint})")
        else:
            col_parts.append(f"{key_mark}`{col.name}`({col.column_type})")
    lines.append(", ".join(col_parts[:column_cap]))
    if len(col_parts) > column_cap:
        lines.append(f"  ...及其他 {len(col_parts) - column_cap} 列")

    # Show NULL columns separately as warnings
    null_cols = [c.name for c in meta.columns if col_profiles.get(c.name, {}).get('value_type') == '全NULL ⚠️']
    if null_cols:
        lines.append(f"⚠️ 全 NULL 列: {', '.join(null_cols[:5])}" + (f" 等{len(null_cols)}列" if len(null_cols) > 5 else ""))

    # Index information (critical for large table query performance)
    if meta.indexes:
        unique_indexes = [idx for idx in meta.indexes if idx.get("unique")]
        non_unique_indexes = [idx for idx in meta.indexes if not idx.get("unique")]
        idx_parts = []
        for idx in unique_indexes:
            col = idx.get("column", "") or idx.get("name", "")
            idx_parts.append(f"⚡`{col}`(唯一)")
        for idx in non_unique_indexes[:6]:  # Cap non-unique indexes shown
            col = idx.get("column", "") or idx.get("name", "")
            idx_parts.append(f"`{col}`")
        if len(non_unique_indexes) > 6:
            idx_parts.append(f"...及其他 {len(non_unique_indexes) - 6} 个索引")
        if idx_parts:
            lines.append(f"📌 索引: {', '.join(idx_parts)}")

    # Sample rows
    if meta.sample_rows:
        lines.append(f"\n抽样 ({len(meta.sample_rows)} 行):")
        sample_cols = list(meta.sample_rows[0].keys())[:5]
        lines.append("| " + " | ".join(sample_cols) + " |")
        lines.append("|" + "|".join(["---"] * len(sample_cols)) + "|")
        for row in meta.sample_rows[:3]:
            formatted = [str(row.get(c, "NULL"))[:25] if row.get(c) is not None else "NULL" for c in sample_cols]
            lines.append("| " + " | ".join(formatted) + " |")
    lines.append("")


def _build_join_reference(snapshot: SchemaSnapshot = None) -> str:
    """Build a concise JOIN reference card."""
    if not snapshot or not snapshot.foreign_keys:
        return ""
    q = db.quote
    lines = ["## JOIN 速查表"]
    for fk in snapshot.foreign_keys:
        lines.append(
            f"JOIN {q(fk.referenced_table)} ON {q(fk.table_name)}.{q(fk.column_name)} "
            f"= {q(fk.referenced_table)}.{q(fk.referenced_column)}"
        )
    return "\n".join(lines)


def _append_transitive_joins(lines: list, foreign_keys: list, tables: dict, q, is_large: bool):
    """Append multi-hop transitive JOIN examples for common patterns."""
    # Build FK adjacency: child_table -> list of FKs
    fk_map = {}
    for fk in foreign_keys:
        fk_map.setdefault(fk.table_name, []).append(fk)

    transitive_paths = []
    for child, fks in list(fk_map.items())[:10]:  # Limit to avoid combinatorial explosion
        for fk in fks[:3]:
            parent = fk.referenced_table
            if parent in fk_map and parent != child:
                for fk2 in fk_map[parent][:2]:
                    grandparent = fk2.referenced_table
                    if grandparent != child and grandparent != parent:
                        transitive_paths.append(
                            f"- `{q(child)}` → `{q(parent)}` → `{q(grandparent)}`\n"
                            f"  `JOIN {q(parent)} ON {q(child)}.{q(fk.column_name)} = {q(parent)}.{q(fk.referenced_column)}`\n"
                            f"  `JOIN {q(grandparent)} ON {q(parent)}.{q(fk2.column_name)} = {q(grandparent)}.{q(fk2.referenced_column)}`"
                        )
                        if len(transitive_paths) >= 5:
                            break
                if len(transitive_paths) >= 5:
                    break
        if len(transitive_paths) >= 5:
            break

    if transitive_paths:
        lines.append("")
        lines.append("**🔗 多表联查示例（2跳路径）**：")
        lines.extend(transitive_paths)


# ---- Agent Prompts ----


def build_explore_agent_prompt() -> str:
    """Prompt for the exploration agent — backend-aware."""
    from app.database.connection import db
    db_name = db.current_db_name
    bt = db.db_type
    if bt in ("dameng", "dm", "dm8"):
        backend_note = ("达梦数据库，Oracle 兼容。没有 information_schema，"
                        "用 ALL_TABLES/ALL_TAB_COLUMNS 系统视图。"
                        "标识符用双引号，不要用 SHOW/DESCRIBE。")
    elif bt == "postgresql":
        backend_note = ("PostgreSQL 数据库。标识符用双引号，"
                        "不要用 SHOW/DESCRIBE。")
    else:
        backend_note = "MySQL 数据库。标识符用反引号。"

    return (
        f"你正在探索一个全新的数据库 '{db_name}'。\n"
        f"数据库类型: {backend_note}\n\n"
        "## 任务\n"
        "1. 使用 list_tables 了解所有表\n"
        "2. 对每张表使用 describe_table 了解结构\n"
        "3. 对重要表使用 sample_data 了解内容\n"
        "4. 生成报告：业务领域、表用途、表关系、关键字段\n\n"
        "## 报告末尾必须包含推荐问题\n"
        "```suggestions\n问题1\n问题2\n问题3\n问题4\n问题5\n```\n"
        "推荐问题应基于实际探索内容，涵盖数据查询、统计、结构等类型。"
    )


def build_analysis_agent_prompt() -> str:
    """Prompt for the Analysis & Planning Agent — explores schema, infers column meaning, creates a query plan.

    This agent does NOT query data. It uses schema exploration tools to understand
    the database structure and produces a plan that the SQL Agent will execute.
    """
    from app.database.connection import db
    bt = db.db_type
    is_pg = (bt == "postgresql")
    is_dm = (bt in ("dameng", "dm", "dm8"))
    db_type_name = "达梦(Dameng)" if is_dm else ("PostgreSQL" if is_pg else "MySQL")
    quote_char = '"' if (is_pg or is_dm) else '`'

    return (
        f"你是 {db_type_name} 数据库分析专家。\n\n"
        "## 工作方式（严格遵守）\n"
        "你的思考顺序必须是：**先认识数据库 → 再理解用户问题 → 最后生成查询计划**。\n"
        "禁止做法：根据用户问题的措辞直接去数据库里找匹配的列名——这必然失败。\n"
        "正确做法：先打开表看实际数据，建立对数据库的认知，然后基于这个认知解读用户问题。\n\n"
        "## 核心原则\n"
        "- **你不执行数据查询** — 你只探索 schema 并生成计划\n"
        "- **最多探索 3 张表** — 使用 get_schema_summary 概览后，只深入分析最相关的 2-3 张表\n"
        "- **以数据库为准** — 这是最重要的原则。你的每一个查询条件都必须基于数据库中实际存在的数据。\n"
        "  工作方式：先探索数据库（看实际数据）→ 理解数据库的存储方式 → 再确定查询方案。\n"
        "  禁止工作方式：先确定查询方案（基于用户措辞）→ 再去数据库验证。\n"
        "  用户词汇和数据库存储常常不同，永远不要假设它们一致。\n"
        "- **列名可能无实际意义** — 必须通过样本数据推断列的真实含义\n"
        "- 例如：`F_STATUS` 样本值 '在职'/'离职' → 员工状态\n"
        "- 例如：`COL_003` 样本值 '技术部'/'人事部' → 部门名称\n"
        "- **避免全量查询** — 计划中必须包含 LIMIT 或聚合，大表优先走索引\n"
        "- **NULL 列必须找替代** — 如果目标列全为 NULL：\n"
        "  1) 先用 get_table_detail 列出该表全部列名和样本值，一次性识别所有可能相关的候选列\n"
        "  2) 按相关度排序：值内容匹配度 > 列名相似度 > 数据类型匹配\n"
        "  3) 从相关度最高的候选列开始，用 analyze_column 确认其值分布\n"
        "  4) 找到有数据的列后立即使用，不要继续扫描\n"
        "- **准确性第一** — 如有不确定性（如两个相似表），在注意事项中说明\n"
        f"- 所有标识符使用 {quote_char} 引用\n"
        "- **用户明确陈述优先** — 用户纠正或补充的事实信息具有最高优先级，直接采信，\n"
        "  不需要再通过其他方式验证。\n"
        "- **搜索现有知识优先** — 遇到含义不明的列时，先使用 search_documentation 工具\n"
        "  搜索知识库。管理员可能已经回答过关于某些列的说明，答案就在知识库中。\n"
        "- **管理员提问** — search_documentation 未找到答案且列的值是真正的编码/缩写\n"
        "  （如 '01'/'02'/'03'、'A'/'B'/'C' 等无中文含义的值），使用 ask_admin 工具向\n"
        "  管理员提问。提问应清晰、包含样本值上下文。\n"
        "- **持续提问，直到完全了解** — 这是最重要的原则之一。你的每一次提问都在逐步完善\n"
        "  数据库知识库。不必限制提问数量——每发现一个含义不明的编码列都应该提问。\n"
        "  目标不是只回答当前问题，而是逐步建立对整库的完整理解。\n"
        "  提问后继续分析当前查询，不要等待——答案会在后续查询中生效。\n\n"
        "## 工作流程（严格按此顺序，不得跳过）\n\n"
        "### 第一步：认识数据库（纯粹探索，不想用户问题）\n"
        "1) 调用 `get_schema_summary` 一次，锁定最相关的 3-4 张表\n"
        "2) 对每张相关表调用 `get_table_detail`，**逐列查看样本值**——不是看列名，是看实际数据内容\n"
        "3) 发现某列有数据（非 NULL），立即记录：列名、实际值长什么样、值格式\n"
        "4) 发现某列全 NULL，立即标注为不可用\n"
        "5) 用 `analyze_column` 确认关键列的值分布\n"
        "**核心心态**：你不是在找「哪列能回答用户问题」，而是在了解「这张表实际存了什么」。\n"
        "同时，每当你发现一个含义不明的编码列，就应该调用 ask_admin 提问——\n"
        "这是持续完善知识库的过程，不受数量限制。\n\n"
        "### 第二步：理解用户问题（基于数据库认识）\n"
        "现在你知道了数据库的真实内容。回头看用户问题中的每个关键词：\n"
        "- 在数据库的实际数据中对应什么？不是字面匹配列名，是匹配值内容\n"
        "- 如果第一步标注了某列全 NULL——立即回到第一步，扫描该表其他列的值，找出实际包含相关数据的列\n"
        "- 地名可能在名字列、编码列、甚至 JSON 字段里——逐列看值，不逐列猜名\n"
        "- 找到候选列后，从最相关的开始用 analyze_column 确认，确认即用，不再扫描\n"
        "- 遇到编码/缩写值无法确定含义时，先用 search_documentation 搜索已有管理员说明；\n"
        "  未找到则 ask_admin 提问（对所有真正编码值都要提问——不限于当前查询相关的列）\n"
        "如需外部知识（地理编码等），此时用 search_web，搜索词用用户的原始表述。\n\n"
        "### 第三步：输出查询计划\n"
        "计划必须基于前两步的成果：第一步对数据库的认识 + 第二步对用户问题的理解。\n"
        "**在收集足够信息后立即输出计划，不要再继续探索。**\n\n"
        "```plan\n"
        "## 数据库认识（实际探索结果）\n"
        "- 表A: 有哪些列、实际值长什么样、哪些列有数据\n"
        "- 表B: 同上\n\n"
        "## 用户问题映射\n"
        "用户说的 [X] → 在数据库中对应 [Y] 列/值，因为 [基于实际数据的证据]\n\n"
        "## 列名推断\n"
        "- `表名`.`列名`: 样本值 [值1, 值2, 值3] → 推断含义为 [含义]\n"
        "  (列名自明则标注\"列名自明\")\n\n"
        "## 相关表及关键列\n"
        "- `表A` ({n}行) — 为什么相关\n"
        "  关键列: col1 (含义), col2 (含义)\n\n"
        "## 查询计划\n"
        "### 步骤 1: [描述]\n"
        "- 查询: `表A` JOIN `表B` ON ...\n"
        "- 选择列: 只选必要的列\n"
        "- 条件: WHERE ...（必须写出具体的条件表达式，基于数据库实际值，不是基于用户措辞）\n"
        "- 如果是统计类问题，必须包含聚合（COUNT/SUM/GROUP BY）或完整的时间范围过滤\n"
        "- 限制: LIMIT 200（计数类用 COUNT(*)，不取明细）\n"
        "### 步骤 2: [如需多步查询才写]\n\n"
        "## JOIN 路径\n"
        "- `表A`.`列` → `表B`.`列`\n\n"
        "## 注意事项\n"
        "- [多表歧义、NULL 处理、数据质量、性能建议]\n"
        "```\n\n"
        "**记住: 最多探索 3 张表，然后立刻输出计划。优先用 COUNT/LIMIT，避免全量查询。**"
    )


def build_sql_agent_prompt(snapshot: SchemaSnapshot = None, exploration_report: str = None, analysis_plan: str = "", table_filter: list = None) -> str:
    """Prompt for the SQL agent — database-type aware, complete context.

    Args:
        snapshot: Schema snapshot for the current database.
        exploration_report: Previous exploration report for business context.
        analysis_plan: Plan from the Analysis Agent — if provided, shown prominently
                       and the SQL agent is told to follow it.
        table_filter: Optional list of allowed table names.
    """
    from app.database.connection import db
    bt = db.db_type
    is_pg = (bt == "postgresql")
    is_dm = (bt in ("dameng", "dm", "dm8"))

    db_type_name = "达梦(Dameng)" if is_dm else ("PostgreSQL" if is_pg else "MySQL")

    # Extract table references from analysis plan for focused context
    focus_tables = _extract_table_refs(analysis_plan) if analysis_plan else None

    parts = [
        f"你是 {db_type_name} 查询专家。根据用户问题生成并执行 SQL。",
        "",
        "## 核心规则（严格遵守）",
        "- 只生成 SELECT，禁止 INSERT/UPDATE/DELETE/DDL",
        f"- 使用 {'双引号' if (is_pg or is_dm) else '反引号'} 引用标识符，使用中文别名",
        "- **必须加 LIMIT**：除非是 COUNT 聚合查询，否则必须 LIMIT 200",
        "- **计数用 COUNT(*)**：'有多少'类问题直接 SELECT COUNT(*)，不要 SELECT * 再数",
        "- **先看数据再查**：写 WHERE 条件前先看相关列的值。如果目标列全 NULL，逐列扫描其他列找数据，不要凭列名相似性猜测替代列",
        "- **先过滤再取数**：WHERE 条件放前面，不要取全表再筛选",
        "- **避免全表扫描**：大表查询必须走索引或 WHERE 条件",
        "- 如果问题涉及计算，查询原始明细数据即可（后续有计算 agent）",
        "- **JOIN 规则**：FK 列标注 ⚠️FK可空 时必须用 LEFT JOIN，非空用 INNER JOIN；按 FK 关系 JOIN，不要臆造关联条件",
        "- **用户陈述优先**：用户在对话中已明确的实时信息直接采信，不重复查询",
        "- **时效性判断**：系统已提供 [系统时间参考]，直接使用其中的具体年份/月份，不需要 search_web 再查时间",
        "- **多表查询**：先确认表间的 FK 路径（参考上方 🔗 外键关系），链式 JOIN 时注意顺序",
        "- 用户问统计/数量/列表时必须用聚合或 WHERE 条件实际查询，不能只看样本就说「无法确认」。结果不理想时重试一次",
        "- **以数据库为准，不以用户措辞为准**：在写 SQL 之前，必须先用 get_table_detail 或 analyze_column 看清相关列的真实数据内容。用户措辞和数据库存储方式常常不同——不要假设它们一致。",
        "- **使用 search_documentation**：对列含义或编码值映射不确定时，先用 search_documentation 搜索已有的管理员说明和文档。这些说明基于真实业务知识，优先级高于基于样本数据的推断。",
        "- **数据准确性第一**：不确定时多查一次确认，不要猜测",
    ]
    if is_pg:
        parts.append("- PostgreSQL：不要用 SHOW/DESCRIBE")
    elif is_dm:
        parts.append("- 达梦：Oracle 兼容语法，用 ROWNUM 或 FETCH FIRST N ROWS ONLY 限制行数")
        parts.append("- 达梦：没有 information_schema，用 ALL_TABLES/ALL_TAB_COLUMNS 系统视图")

    # Analysis plan (from Analysis Agent) — show it first and prominently
    if analysis_plan:
        parts.append("## 📋 查询计划（由 Analysis Agent 生成）")
        parts.append("**请遵循以下计划生成 SQL。计划中的列名推断基于实际数据，请信任这些推断。**")
        parts.append(analysis_plan)
        parts.append("---")

    # Schema context — adaptive: focuses on plan-referenced tables for large DBs
    schema_text = _build_schema_context(snapshot, focus_tables, table_filter)
    if schema_text:
        parts.append(schema_text)

    # Exploration report (business context from previous exploration)
    if exploration_report:
        # Truncate to avoid overwhelming the context
        report_short = exploration_report[:1500]
        parts.append(f"## 数据库业务说明（来自探索报告）\n{report_short}")

    return "\n\n".join(parts)


def build_compute_agent_prompt(
    user_question: str,
    query_results: list,
    chart_strategy: str = "",
    enable_charts: bool = False,
) -> str:
    """Prompt for the Python compute/visualization agent."""
    results_text = _format_query_results_for_prompt(query_results)
    parts = [
        "你是 Python 计算" + ("和图表生成专家。" if enable_charts else "专家。") +
        "对查询结果进行计算" + ("并生成可视化图表。" if enable_charts else "。"),
        "",
        f"## 用户问题\n{user_question}",
        "",
        f"## 查询结果\n{results_text}",
        "",
        "## 规则",
        "- 使用 run_python 执行计算，input_data['queries'][N]['rows'] 是 dict 列表",
        "- 结果赋值给 result 变量",
        "- 可用: math, statistics, json, collections, itertools, matplotlib",
    ]
    if enable_charts:
        parts.extend([
            "- 计算完成后停止，不解释结果",
            "",
            "## 图表生成规则",
            f"图表策略提示: {chart_strategy}" if chart_strategy else "",
            "- 导入顺序（必须严格遵守）:",
            "  import matplotlib",
            "  matplotlib.use('Agg')",
            "  import matplotlib.pyplot as plt",
            "- 设置中文字体:",
            "  plt.rcParams['font.sans-serif'] = ['Sarasa Gothic SC']",
            "  plt.rcParams['axes.unicode_minus'] = False",
            "- 图表保存: plt.savefig('chart_<描述>.png', dpi=100, bbox_inches='tight')",
            "- 保存后调用 plt.close() 释放内存",
            "- 将图表文件名放入 result['charts'] 列表: result = {'charts': ['chart_xxx.png'], ...}",
            "- 每个图表单独创建 figure (plt.figure())，不要使用 subplots 组合多图",
            "- 图表标题、轴标签使用中文",
            "- 数据点较多时只取 Top 10-15 展示，避免图表过于拥挤",
        ])
    else:
        parts.append("- 计算完成后停止，不解释结果")
    return "\n".join(parts)


def build_visualization_agent_prompt(
    user_question: str,
    query_results: list,
    chart_strategy: str = "",
) -> str:
    """Prompt for the dedicated Visualization Agent — charts ONLY, no computation."""
    results_text = _format_query_results_for_prompt(query_results)
    parts = [
        "你是数据可视化专家。你的唯一任务是根据查询结果生成图表。",
        "不要进行任何数学计算或数据分析 — 只生成图表！",
        "",
        f"## 用户问题\n{user_question}",
        "",
        f"## 查询结果\n{results_text}",
        "",
        "## 图表生成规则",
        "- 使用 run_python 执行可视化代码",
        "- input_data['queries'][N]['rows'] 是 dict 列表（key=列名, value=值）",
        "- 注意：日期列的值是 datetime.date 对象，使用 .year / .month / .strftime() 而不是字符串切片 [:4]",
        "- 数值列的值可能是 Decimal 类型，使用 int() 或 float() 转换",
        "- input_data['queries'][N]['raw_rows'] 是 list 列表，用于快速提取数据列",
        "- 导入顺序（必须严格遵守）:",
        "  import matplotlib",
        "  matplotlib.use('Agg')",
        "  import matplotlib.pyplot as plt",
        "- 设置中文字体（必须）:",
        "  plt.rcParams['font.sans-serif'] = ['Sarasa Gothic SC']",
        "  plt.rcParams['axes.unicode_minus'] = False",
        "- 图表保存: plt.savefig('chart_<描述>.png', dpi=100, bbox_inches='tight')",
        "- 保存后调用 plt.close() 释放内存",
        "- 将图表文件名放入 result: result = {'charts': ['chart_xxx.png']}",
        "- 每个图表单独创建 figure，不要使用 subplots 组合多图",
        "- 图表标题、轴标签使用中文",
        "- 数据点多时只取 Top 10-15 展示",
        "",
        "## 图表类型选择",
        "- 类别+数值（各部门人数、各考试通过率） → 柱状图 plt.bar()",
        "- 占比数据（各部分比例，总和有意义） → 饼图 plt.pie()",
        "- 时间序列（日期/年份X轴，数值Y轴） → 折线图 plt.plot()",
        "- 两个数值列的关系 → 散点图 plt.scatter()",
        "- 数值分布（多行单一数值列） → 直方图 plt.hist()",
        "",
        "## 代码模板",
        "```python",
        "import matplotlib",
        "matplotlib.use('Agg')",
        "import matplotlib.pyplot as plt",
        "plt.rcParams['font.sans-serif'] = ['Sarasa Gothic SC']",
        "plt.rcParams['axes.unicode_minus'] = False",
        "",
        "rows = input_data['queries'][0]['rows']",
        "# 日期列使用 .strftime() 或 .year（不是 [:4] 切片）",
        "# 数值列使用 int() 或 float() 转换",
        "labels = [r['name_column'] for r in rows]",
        "values = [float(r['value_column']) for r in rows]",
        "",
        "plt.figure(figsize=(10, 6))",
        "plt.bar(labels, values)",
        "plt.title('图表标题', fontsize=14)",
        "plt.xlabel('X轴标签')",
        "plt.ylabel('Y轴标签')",
        "plt.xticks(rotation=45)",
        "plt.tight_layout()",
        "plt.savefig('chart_bar.png', dpi=100, bbox_inches='tight')",
        "plt.close()",
        "",
        "result = {'charts': ['chart_bar.png']}",
        "```",
        "",
        "## 重要",
        "- 只生成图表，不解释、不分析、不计算",
        "- 数据不适合任何图表时，设置 result = {}（空 dict）",
        "- 生成完成后立即停止",
    ]
    if chart_strategy:
        parts.insert(4, f"## 图表策略（来自规划阶段）\n{chart_strategy}")
    return "\n".join(parts)


def build_nl_agent_prompt(
    user_question: str,
    query_results: list = None,
    computed_results: list = None,
    chart_images: list = None,
) -> str:
    """Prompt for the NL response agent — structured, no hallucination."""
    results_text = _format_query_results_for_prompt(query_results or [])

    parts = [
        "你是数据分析师。基于查询结果用中文回答用户问题。",
        f"## 用户问题\n{user_question}",
    ]

    if results_text:
        parts.append(f"## 查询结果\n{results_text}")

    if computed_results:
        parts.append(f"## 计算结果\n{_format_computed_results(computed_results)}")

    if chart_images:
        parts.append(f"## 生成的图表\n已生成 {len(chart_images)} 张图表，将展示在回答中。可以在回答中引用图表（如「如下图所示」）。")

    parts.extend([
        "",
        "## 回答规则（严格遵守）",
        "- **只基于上面的数据回答，绝对不要编造或猜测**",
        "- **禁止输出 SQL 语句、Python 代码、或任何代码块**",
        "- **禁止输出以 SELECT、FROM、WHERE、JOIN、GROUP BY 等关键字开头或包含的文本**",
        "- 纯自然语言描述，不要提\"执行查询\"或\"运行SQL\"",
        "- 数据为空时如实告知（例如\"查询结果为空，没有匹配的记录\"），不要猜测原因",
        "- **数字精确引用**，不四舍五入，不近似",
        "- **如有不确定性，明确指出**（例如\"数据库中有两个员工表，本结果基于 employees 表\"）",
        "- 末尾标注\"来源: 数据库查询\"",
    ])

    return "\n\n".join(parts)


# ---- Legacy compat ----

def build_system_prompt(classification: str, conversation_history: list = None, snapshot: SchemaSnapshot = None) -> str:
    """Legacy system prompt for special cases."""
    parts = []
    if snapshot:
        schema_text = _build_schema_context(snapshot)
        if schema_text:
            parts.append(schema_text)
    if classification == "DOC_QUESTION":
        parts.append("使用 search_documentation 查找信息，引用文档来源。")
    return "\n\n".join(parts)


# ---- Shared Helpers ----

def _format_query_results_for_prompt(query_results: list) -> str:
    """Format query results — handles tabular, exploration, and structured results."""
    if not query_results:
        return "（暂无查询结果）"

    parts = []
    for i, qr in enumerate(query_results):
        parts.append(f"### 查询 {i + 1}")
        # Don't show SQL to NL agent — it tends to repeat it
        result = qr.get("result", qr)

        if result.get("exploration_report"):
            parts.append("数据库探索报告：")
            parts.append(str(result["exploration_report"])[:2000])
        elif result.get("columns") and result.get("rows"):
            parts.append(f"返回 {result.get('row_count', len(result['rows']))} 行")
            cols = result["columns"]
            parts.append("| " + " | ".join(str(c) for c in cols) + " |")
            parts.append("|" + "|".join(["---"] * len(cols)) + "|")
            for row in result["rows"][:50]:  # max 50 rows display
                formatted = [str(v)[:50] if v is not None else "NULL" for v in row]
                parts.append("| " + " | ".join(formatted) + " |")
        elif result.get("tables"):
            parts.append(f"共 {result.get('count', len(result['tables']))} 张表: {', '.join(result['tables'])}")
        elif result.get("table_name"):
            parts.append(f"表 {result['table_name']}, {result.get('row_count', '?')} 列")
            if result.get("foreign_keys"):
                parts.append("外键: " + "; ".join(result["foreign_keys"]))
        else:
            parts.append(str(result)[:300])
        parts.append("")
    return "\n".join(parts)


def _format_computed_results(computed_results: list) -> str:
    """Format computed results."""
    if not computed_results:
        return ""
    parts = []
    for i, cr in enumerate(computed_results):
        parts.append(f"### 计算 {i + 1}")
        parts.append(cr.get("content", str(cr))[:1000])
        parts.append("")
    return "\n".join(parts)
