"""结构类问题的判定与直接作答（不走模型）。

`_is_structure_question` 在历史实现里存在两份：
`app/agent/orchestrator.py:1643`（结构关键词 + 排除数据关键词）与
`app/agent/router.py:105`（另一份结构关键词表）。两份关键词表互有出入，
同一个问题在"走不走快路径"和"分类成什么"两处的判定不一致。

这里合并成一份：关键词取两者的并集，且保留"在问数据记录就不是结构问题"
的反向过滤。分类器（`agent.classifier`）与聊天管道都引它。
"""

from __future__ import annotations

import logging
import re

from dbchat.db.dialects.base import Dialect
from dbchat.domain.schema import CatalogScope, SchemaSnapshot

logger = logging.getLogger(__name__)

__all__ = [
    "COMPLEX_INDICATORS",
    "DATA_INDICATORS",
    "SIMPLE_QUERY_PATTERNS",
    "STRUCTURE_KEYWORDS",
    "build_structure_response",
    "is_simple_query",
    "is_structure_question",
]

#: 指向"数据库自身结构"的关键词（两份历史表的并集）。
STRUCTURE_KEYWORDS: tuple[str, ...] = (
    "有哪些表", "什么表", "表结构", "数据库架构", "有哪些字段",
    "有哪些列", "什么字段", "什么列", "包含哪些表", "数据库结构",
    "介绍一下数据库", "数据库介绍", "schema", "有哪些数据表",
    "数据库里有", "数据库包含", "表关系", "外键关系", "数据库设计",
    "字段是", "列是", "主键", "外键", "索引", "约束", "架构",
    "介绍一下", "包含哪些", "表有哪些", "设计", "结构", "模式", "关系",
)

#: 明确在问数据记录 —— 出现这些就不是"纯结构问题"。
DATA_INDICATORS: tuple[str, ...] = (
    "查询", "统计", "多少", "谁", "哪些人", "成绩", "缺考", "参加", "报名", "名单", "分数",
)

#: 简单查询的模式（可以跳过分析 agent，直接生成 SQL）。
SIMPLE_QUERY_PATTERNS: tuple[str, ...] = (
    r"有多少\S*(人|个|条|张|名|位|次)",
    r"\S+的数量",
    r"有几个\S+",
    r"一共\S*多少",
    r"列出所有\S+",
    r"所有\S+的列表",
    r"查看\S+表",
    r"显示\S+表",
    r"^(员工|部门|课程|考试|培训).{0,5}(列表|有哪些|多少)",
    r"^(查询|查)\s*\S+(表|\s*$)",
)

#: 一出现就说明需要分析（多表、聚合、对比）。
COMPLEX_INDICATORS: tuple[str, ...] = (
    "对比", "比较", "关联", "统计", "分析", "趋势", "变化",
    "排名", "排序", "占比", "比率", "百分比", "平均",
    "同时", "并且", "以及", "还有", "另外",
    "哪些人既", "既.*又", "不.*也不",
    "分别", "各自", "每.*的",
)

#: 短问题里也要走分析的关键词（实体映射容易猜错）。
_SHORT_ENTITY_INDICATORS: tuple[str, ...] = (
    "成绩", "分数", "培训", "考试", "证书", "缺考", "缺席",
)

_SHORT_QUESTION_LENGTH = 8


def is_structure_question(question: str) -> bool:
    """是否**纯**结构问题（可以完全用缓存作答）。

    先要命中结构关键词，再排除"在问数据记录"的情形。
    """
    if not question:
        return False
    lowered = question.strip().lower()
    if not any(keyword in lowered for keyword in STRUCTURE_KEYWORDS):
        return False
    return not any(indicator in lowered for indicator in DATA_INDICATORS)


def is_simple_query(question: str) -> bool:
    """是否简单到可以跳过分析 agent。

    简单 = 意图无歧义（计数、列表、展示）且目标明确。复杂指标一出现就
    一律走分析（多轮对话里"简单问题"往往只是看起来简单）。
    """
    if not question:
        return False
    text = question.strip()
    if any(indicator in text for indicator in COMPLEX_INDICATORS):
        return False
    if any(re.search(pattern, text) for pattern in SIMPLE_QUERY_PATTERNS):
        return True
    return len(text) < _SHORT_QUESTION_LENGTH and not any(
        indicator in text for indicator in _SHORT_ENTITY_INDICATORS
    )


def build_structure_response(
    snapshot: SchemaSnapshot | None,
    *,
    scope: CatalogScope | None = None,
    exploration_report: str = "",
    dialect: Dialect | None = None,
) -> str:
    """直接用缓存渲染一份结构说明（不调模型）。

    历史实现从 `schema_manager` 的 `.summary.table_summaries` 里取值分布，
    那份模型已经不存在了；取值分布改从 `ColumnMeta.sample_values` 读，
    数据来源与内省一致。
    """
    if snapshot is None or not snapshot.table_count:
        return ""

    scope = scope or CatalogScope()
    tables = (
        dict(snapshot.tables)
        if scope.tables is None
        else {name: meta for name, meta in snapshot.tables.items() if scope.allows(name)}
    )
    if not tables:
        return ""

    lines = [f"## {snapshot.db_name} 数据库架构\n"]
    lines.append(f"共 **{len(tables)}** 张表：\n")
    lines.append("| 表名 | 行数 | 列数 | 说明 |")
    lines.append("|------|------|------|------|")
    for name, meta in tables.items():
        rows = meta.row_count if meta.row_count is not None else "?"
        lines.append(f"| {name} | {rows} | {len(meta.columns)} | {meta.comment or '-'} |")

    foreign_keys = [
        fk
        for fk in snapshot.foreign_keys
        if fk.table in tables and scope.allows(fk.ref_table)
    ]
    if foreign_keys:
        lines.append("\n### 表关系\n")
        quote = dialect.quote if dialect is not None else (lambda name: f"`{name}`")
        for fk in foreign_keys:
            lines.append(
                f"- {quote(fk.table)}.{quote(fk.column)} → "
                f"{quote(fk.ref_table)}.{quote(fk.ref_column)}"
            )

    distributions: list[str] = []
    for name, meta in tables.items():
        entries = [
            f"  - `{column.name}`: {', '.join(str(v)[:20] for v in column.sample_values[:5])}"
            for column in meta.columns
            if column.sample_values
        ]
        if entries:
            distributions.append(f"**{name}**（{meta.row_count if meta.row_count is not None else '?'} 行）:")
            distributions.extend(entries)
    if distributions:
        lines.append("\n### 数据分布\n")
        lines.extend(distributions)

    if exploration_report:
        lines.append(f"\n### 探索报告\n{exploration_report[:2000]}")

    lines.append("\n*来源: 数据库 schema 缓存*")
    return "\n".join(lines)
