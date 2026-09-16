"""各 agent 的系统提示词与用户消息拼装（从 `app/agent/prompt_builder.py` 拆分）。

拆分原则：**一个 agent 一个函数**，函数只依赖显式传入的参数。历史实现里
每个 prompt 函数都从全局连接对象上读 `db.db_type` / `db.current_db_name`，
于是同一个函数在不同请求下返回不同结果，无法测试也无法复用。

方言相关的硬编码（"用反引号"/"不要用 SHOW"）全部改为
`Dialect.prompt_notes()` + `Dialect.sql_dialect_hint()`。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from dbchat.db.dialects.base import Dialect
from dbchat.domain.query import Artifact, QueryResult

logger = logging.getLogger(__name__)

__all__ = [
    "RETRY_STRUCTURE_NOTE",
    "build_analysis_history_block",
    "build_analysis_prompt",
    "build_analysis_user_message",
    "build_answer_prompt",
    "build_compute_prompt",
    "build_explore_prompt",
    "build_nl_history",
    "build_sql_prompt",
    "build_visualize_prompt",
    "format_computed_results",
    "format_query_results",
]

_CJK_FONT = "Sarasa Gothic SC"
#: 进入用户消息的历史条数上限（分析 agent 的 Q/A 摘要用）。
_HISTORY_ASSISTANT_TRUNCATE = 200
#: 带进重试轮的"上一轮结论"上限（查询计划通常几百字，2000 足够）。
_PRIOR_FINDINGS_CHARS = 2000

#: 重试轮的结构说明。**不是**禁用工具（丢了上下文的 agent 仍然要能去看），
#: 而是明确告诉它：结构在上轮已经确认过，别再从零探索一遍。
#: 背景：`empty_result` 用例 5 轮里跑了 21 次结构类调用，其中每轮的
#: `get_schema_summary` + `describe_table` + `analyze_column` 组合完全一致。
RETRY_STRUCTURE_NOTE = (
    "本轮是重试轮：表结构、列含义与样本值都已在上一轮确认过"
    "（见下方「上一轮已确认的结论」）。\n"
    "**不要重复调用 get_schema_summary / list_tables / describe_table / "
    "get_table_detail / analyze_column 重新确认同一批表**；\n"
    "只有上一轮结论中确实缺失的表或列，才做**一次**针对性的查看。"
    "请直接针对 [上轮提示] 修正查询口径。"
)


def _dialect_notes(dialect: Dialect | None, limit: int = 4) -> str:
    """把方言注意事项渲染成提示词里的一行。"""
    if dialect is None:
        return ""
    notes = dialect.prompt_notes()
    if not notes:
        return ""
    return "- " + "\n- ".join(notes[:limit])


def build_explore_prompt(dialect: Dialect | None, db_name: str) -> str:
    """探索 agent —— 首次连接一个全新数据库时使用。"""
    backend = dialect.sql_dialect_hint() if dialect is not None else f"数据库 '{db_name}'。"
    return (
        f"你正在探索一个全新的数据库 '{db_name}'。\n"
        f"数据库类型: {backend}\n"
        f"{_dialect_notes(dialect)}\n\n"
        "## 任务\n"
        "1. 使用 list_tables 了解所有表\n"
        "2. 对每张表使用 describe_table 了解结构\n"
        "3. 对重要表使用 sample_data 了解内容\n"
        "4. 生成报告：业务领域、表用途、表关系、关键字段\n\n"
        "## 报告末尾必须包含推荐问题\n"
        "```suggestions\n问题1\n问题2\n问题3\n问题4\n问题5\n```\n"
        "推荐问题应基于实际探索内容，涵盖数据查询、统计、结构等类型。"
    )


def build_analysis_prompt(dialect: Dialect | None) -> str:
    """分析 / 规划 agent —— 只探索结构、产出计划，不取数。"""
    type_name = dialect.display_name if dialect is not None else "当前"
    quote_label = dialect.quote_label_zh if dialect is not None else "当前数据库的引用字符"
    return (
        f"你是 {type_name} 数据库分析专家。\n\n"
        "## 工作方式（严格遵守）\n"
        "你的思考顺序必须是：**先认识数据库 → 再理解用户问题 → 最后生成查询计划**。\n"
        "禁止做法：根据用户问题的措辞直接去数据库里找匹配的列名 —— 这必然失败。\n"
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
        f"- 所有标识符使用{quote_label}引用\n"
        "- **用户明确陈述优先** — 用户纠正或补充的事实信息具有最高优先级，直接采信。\n"
        "- **搜索现有知识优先** — 遇到含义不明的列时，先使用 search_documentation 工具\n"
        "  搜索知识库。管理员可能已经回答过关于某些列的说明，答案就在知识库中。\n"
        "- **管理员提问** — search_documentation 未找到答案且列的值是真正的编码/缩写\n"
        "  （如 '01'/'02'/'03'、'A'/'B'/'C' 等无中文含义的值），使用 ask_admin 工具向\n"
        "  管理员提问。提问应清晰、包含样本值上下文。\n"
        "- **持续提问，直到完全了解** — 每发现一个含义不明的编码列都应该提问。\n"
        "  提问后继续分析当前查询，不要等待 —— 答案会在后续查询中生效。\n\n"
        "## 工作流程（严格按此顺序，不得跳过）\n\n"
        "### 第一步：认识数据库（纯粹探索，不想用户问题）\n"
        "1) 调用 `get_schema_summary` 一次，锁定最相关的 3-4 张表\n"
        "2) 对每张相关表调用 `get_table_detail`，**逐列查看样本值** —— 不是看列名，是看实际数据内容\n"
        "3) 发现某列有数据（非 NULL），立即记录：列名、实际值长什么样、值格式\n"
        "4) 发现某列全 NULL，立即标注为不可用\n"
        "5) 用 `analyze_column` 确认关键列的值分布\n\n"
        "### 第二步：理解用户问题（基于数据库认识）\n"
        "- 在数据库的实际数据中对应什么？不是字面匹配列名，是匹配值内容\n"
        "- 如果第一步标注了某列全 NULL —— 立即回到第一步，扫描该表其他列的值\n"
        "- 找到候选列后，从最相关的开始用 analyze_column 确认，确认即用，不再扫描\n"
        "如需外部知识（地理编码等），此时用 search_web，搜索词用用户的原始表述。\n\n"
        "### 第三步：输出查询计划\n"
        "**在收集足够信息后立即输出计划，不要再继续探索。**\n\n"
        "```plan\n"
        "## 数据库认识（实际探索结果）\n"
        "- 表A: 有哪些列、实际值长什么样、哪些列有数据\n\n"
        "## 用户问题映射\n"
        "用户说的 [X] → 在数据库中对应 [Y] 列/值，因为 [基于实际数据的证据]\n\n"
        "## 列名推断\n"
        "- `表名`.`列名`: 样本值 [值1, 值2] → 推断含义为 [含义]\n\n"
        "## 相关表及关键列\n"
        "- `表A` (N行) — 为什么相关\n"
        "  关键列: col1 (含义), col2 (含义)\n\n"
        "## 查询计划\n"
        "### 步骤 1: [描述]\n"
        "- 查询: `表A` JOIN `表B` ON ...\n"
        "- 条件: WHERE ...（必须写出具体条件表达式，基于数据库实际值）\n"
        "- 限制: LIMIT 200（计数类用 COUNT(*)，不取明细）\n\n"
        "## JOIN 路径\n"
        "- `表A`.`列` → `表B`.`列`\n\n"
        "## 注意事项\n"
        "- [多表歧义、NULL 处理、数据质量、性能建议]\n"
        "```\n\n"
        "**记住: 最多探索 3 张表，然后立刻输出计划。优先用 COUNT/LIMIT，避免全量查询。**"
    )


def build_analysis_user_message(
    question: str,
    *,
    history_block: str = "",
    rag_context: str = "",
    prior_findings: str = "",
    retry: bool = False,
) -> str:
    """分析 agent 的用户消息（问题 + 历史 + RAG 上下文 + 重试轮的既有结论）。

    `retry=True` 时带上上一轮的结论，并说明结构已经确认过 —— 每轮都从头
    重跑一遍 `get_schema_summary` / `describe_table` / `analyze_column`
    是纯浪费（用例层不再给重试轮开一份全新的空白分析 agent）。
    """
    message = f"请分析以下用户问题，探索相关数据库结构，并生成查询计划：\n\n**用户问题**: {question}\n\n"
    if history_block:
        message += (
            "**对话历史**（本轮之前的问题和回答，用于理解上下文和代词引用）:\n"
            f"{history_block}\n\n"
        )
    if rag_context:
        message += (
            "## 相关知识库内容（文档 + 管理员说明）\n\n"
            f"{rag_context}\n\n"
            "请优先使用以上知识库内容来理解列含义和编码值映射。\n\n"
        )
    if retry:
        if prior_findings:
            message += (
                "## 上一轮已确认的结论（本轮不要重复确认）\n"
                f"{prior_findings[:_PRIOR_FINDINGS_CHARS]}\n\n"
            )
        message += f"{RETRY_STRUCTURE_NOTE}\n\n"
        message += (
            "最终计划放在 ```plan 代码块中，"
            "只需要给出**修正后的**查询步骤，不必重述整个数据库结构。"
        )
        return message
    message += (
        "请快速完成分析：概览 → 深入 2-3 张相关表 → 立即输出计划。"
        "最终计划放在 ```plan 代码块中。"
    )
    return message


def build_analysis_history_block(history: Sequence[dict]) -> str:
    """把对话历史压成编号 Q/A（帮助分析 agent 消解代词、避免重复探索）。"""
    if not history:
        return ""
    filtered: list[tuple[str, str]] = []
    for message in history:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            filtered.append((role, content))
    if not filtered:
        return ""

    lines: list[str] = []
    turn = 1
    for role, content in filtered:
        if role == "user":
            lines.append(f"Q{turn}: {content}")
        elif role == "assistant":
            truncated = (
                content[:_HISTORY_ASSISTANT_TRUNCATE] + "..."
                if len(content) > _HISTORY_ASSISTANT_TRUNCATE
                else content
            )
            lines.append(f"A{turn}: {truncated}")
            turn += 1
    return "\n".join(lines)


def build_nl_history(history: Sequence[dict]) -> list[dict[str, str]]:
    """给"只吃纯文本"的 agent 用的精简历史（丢掉工具调用等中间态）。"""
    out: list[dict[str, str]] = []
    for message in history:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, str) and content.strip() and role in ("user", "assistant"):
            out.append({"role": role, "content": content})
    return out


def build_sql_prompt(
    *,
    dialect: Dialect | None,
    schema_context: str = "",
    exploration_report: str = "",
    analysis_plan: str = "",
) -> str:
    """SQL agent 的系统提示词。"""
    type_name = dialect.display_name if dialect is not None else "当前"
    quote_label = dialect.quote_label_zh if dialect is not None else "当前数据库的引用字符"
    row_hint = dialect.limit_clause(200) if dialect is not None else "LIMIT 200"

    parts = [
        f"你是 {type_name} 查询专家。根据用户问题生成并执行 SQL。",
        "",
        "## 核心规则（严格遵守）",
        "- 只生成 SELECT，禁止 INSERT/UPDATE/DELETE/DDL",
        f"- 使用{quote_label}引用标识符，使用中文别名",
        f"- **必须限制返回行数**：除非是 COUNT 聚合查询，否则必须写成 {row_hint}",
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
        "- **以数据库为准，不以用户措辞为准**：写 SQL 之前，必须先用 get_table_detail 或 analyze_column 看清相关列的真实数据内容",
        "- **使用 search_documentation**：对列含义或编码值映射不确定时，先搜索已有的管理员说明；其优先级高于基于样本数据的推断",
        "- **数据准确性第一**：不确定时多查一次确认，不要猜测",
    ]
    notes = _dialect_notes(dialect, limit=5)
    if notes:
        parts.append("## 方言注意事项")
        parts.append(notes)

    if analysis_plan:
        parts.append("## 📋 查询计划（由 Analysis Agent 生成）")
        parts.append("**请遵循以下计划生成 SQL。计划中的列名推断基于实际数据，请信任这些推断。**")
        parts.append(analysis_plan)
        parts.append("---")

    if schema_context:
        parts.append(schema_context)

    if exploration_report:
        parts.append(f"## 数据库业务说明（来自探索报告）\n{exploration_report[:1500]}")

    return "\n\n".join(parts)


def build_compute_prompt(
    question: str,
    results: Sequence[QueryResult],
    *,
    strategy: str = "",
    enable_charts: bool = False,
) -> str:
    """计算 agent 的系统提示词。"""
    parts = [
        "你是 Python 计算" + ("和图表生成专家。" if enable_charts else "专家。")
        + "对查询结果进行计算" + ("并生成可视化图表。" if enable_charts else "。"),
        "",
        f"## 用户问题\n{question}",
        "",
        f"## 查询结果\n{format_query_results(results)}",
        "",
        "## 规则",
        "- 使用 run_python 执行计算，input_data['queries'][N]['rows'] 是 dict 列表",
        "- 结果赋值给 result 变量",
        "- 可用: math, statistics, json, collections, itertools, matplotlib",
        "- 计算完成后停止，不解释结果",
    ]
    if strategy:
        parts.append(f"规划建议: {strategy}")
    if enable_charts:
        parts.extend(_chart_rules(strategy))
    return "\n".join(parts)


def build_visualize_prompt(
    question: str,
    results: Sequence[QueryResult],
    *,
    chart_strategy: str = "",
) -> str:
    """可视化 agent —— 只画图，不做计算。"""
    parts = [
        "你是数据可视化专家。你的唯一任务是根据查询结果生成图表。",
        "不要进行任何数学计算或数据分析 — 只生成图表！",
        "",
        f"## 用户问题\n{question}",
        "",
        f"## 查询结果\n{format_query_results(results)}",
        "",
    ]
    if chart_strategy:
        parts.extend([f"## 图表策略（来自规划阶段）\n{chart_strategy}", ""])
    parts.extend(
        [
            "## 图表生成规则",
            "- 使用 run_python 执行可视化代码",
            "- input_data['queries'][N]['rows'] 是 dict 列表（key=列名, value=值）",
            "- 注意：日期列的值是 datetime 对象，使用 .year / .month / .strftime() 而不是字符串切片 [:4]",
            "- 数值列的值可能是 Decimal 类型，使用 int() 或 float() 转换",
            "- input_data['queries'][N]['raw_rows'] 是 list 列表，用于快速提取数据列",
            "",
            "## 图表类型选择",
            "- 类别+数值（各部门人数、各考试通过率） → 柱状图 plt.bar()",
            "- 占比数据（各部分比例，总和有意义） → 饼图 plt.pie()",
            "- 时间序列（日期/年份X轴，数值Y轴） → 折线图 plt.plot()",
            "- 两个数值列的关系 → 散点图 plt.scatter()",
            "- 数值分布（多行单一数值列） → 直方图 plt.hist()",
            "",
            *_chart_rules(chart_strategy),
            "",
            "## 重要",
            "- 只生成图表，不解释、不分析、不计算",
            "- 数据不适合任何图表时，设置 result = {}（空 dict）",
            "- 生成完成后立即停止",
        ]
    )
    return "\n".join(parts)


def _chart_rules(chart_strategy: str) -> list[str]:
    rules = [
        "## 图表生成规则",
        "- 导入顺序（必须严格遵守）:",
        "  import matplotlib",
        "  matplotlib.use('Agg')",
        "  import matplotlib.pyplot as plt",
        "- 设置中文字体（必须）:",
        f"  plt.rcParams['font.sans-serif'] = ['{_CJK_FONT}']",
        "  plt.rcParams['axes.unicode_minus'] = False",
        "- 图表保存: plt.savefig('chart_<描述>.png', dpi=100, bbox_inches='tight')",
        "- 保存后调用 plt.close() 释放内存",
        "- 将图表文件名放入 result: result = {'charts': ['chart_xxx.png'], ...}",
        "- 每个图表单独创建 figure (plt.figure())，不要使用 subplots 组合多图",
        "- 图表标题、轴标签使用中文",
        "- 数据点较多时只取 Top 10-15 展示，避免图表过于拥挤",
    ]
    if chart_strategy:
        rules.append(f"图表策略提示: {chart_strategy}")
    return rules


def build_answer_prompt(
    question: str,
    results: Sequence[QueryResult],
    *,
    computed: Sequence[str] = (),
    artifacts: Sequence[Artifact] = (),
) -> str:
    """最终回答 agent（无工具，只把已有结果说清楚）。"""
    parts = [
        "你是数据分析师。基于查询结果用中文回答用户问题。",
        f"## 用户问题\n{question}",
    ]
    results_text = format_query_results(results)
    if results_text:
        parts.append(f"## 查询结果\n{results_text}")
    if computed:
        parts.append(f"## 计算结果\n{format_computed_results(computed)}")
    if artifacts:
        parts.append(
            f"## 生成的图表\n已生成 {len(artifacts)} 张图表，将展示在回答中。"
            "可以在回答中引用图表（如「如下图所示」）。"
        )
    parts.extend(
        [
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
        ]
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 结果渲染


def format_query_results(results: Sequence[QueryResult]) -> str:
    """把查询结果渲染成提示词里的表格。

    刻意**不**展示 SQL：历史实现把 SQL 一起给了回答 agent，回答里就开始
    复述 SQL（后来不得不加 `_strip_sql_from_text` 去正则清洗）。
    """
    if not results:
        return "（暂无查询结果）"
    parts: list[str] = []
    for index, result in enumerate(results, 1):
        parts.append(f"### 查询 {index}")
        if not result.columns:
            parts.append("（该语句没有返回结果集）")
        elif result.row_count == 0:
            parts.append("查询执行成功，但没有匹配的数据（0 行）")
        else:
            parts.append(f"返回 {result.row_count} 行")
            columns = [str(column) for column in result.columns]
            parts.append("| " + " | ".join(columns) + " |")
            parts.append("|" + "|".join(["---"] * len(columns)) + "|")
            for row in result.rows[:50]:
                values = [
                    "NULL" if row.get(column) is None else str(row.get(column))[:50]
                    for column in result.columns
                ]
                parts.append("| " + " | ".join(values) + " |")
        parts.append("")
    return "\n".join(parts)


def format_computed_results(computed: Sequence[str]) -> str:
    """计算结果的文本汇总。"""
    if not computed:
        return ""
    parts: list[str] = []
    for index, content in enumerate(computed, 1):
        parts.append(f"### 计算 {index}")
        parts.append(str(content)[:1000])
        parts.append("")
    return "\n".join(parts)
