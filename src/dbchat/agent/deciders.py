"""结果评估与计算规划 —— 两次轻量的模型判定。

两条都是**非流式**调用（`LLMClient.complete`，全程 `await`）。历史实现
（`orchestrator.py:506-522` 与 `:1538-1556`）直接调用同步的
`client.messages.create()` / `chat.completions.create()` —— 在事件循环线程上
做网络 IO，一次判定就把所有并发请求堵住。

**缺陷修复（评估看不到"没查"）**：历史 `_evaluate_results` 只判断
`round_results` 是否为空：

    if not query_results:
        return {"satisfactory": False, "reason": "查询未返回任何结果", ...}

于是"这一轮根本没执行查询"和"查询执行了但确实没有数据"被当成同一件事。
抓到的基线里，一个 PostgreSQL 问题**烧掉了 5 轮中的 4 轮、112 秒**：SQL
agent 每轮只调 `get_schema_summary`，评估器却一直提示"查询未返回任何结果"，
模型于是又去"确认表结构"。现在把 `RunContext.ran_any_query` 喂进评估：

* 没查过 → 提示"你还没有执行任何查询，请用 query_database 查询"；
* 查了但为空 → 提示换口径（换条件/换列/换表），不要重复确认结构。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from dbchat.domain.errors import LlmError
from dbchat.domain.query import QueryResult
from dbchat.llm.client import LLMClient

__all__ = [
    "ComputationPlan",
    "Evaluation",
    "evaluate_results",
    "plan_computation",
]

logger = logging.getLogger(__name__)

#: 评估与规划只需要一行结论。
_DECIDE_MAX_TOKENS = 200
_PLAN_MAX_TOKENS = 150

_EVALUATOR_SYSTEM = "You are a query result evaluator. Respond with one line."
_PLANNER_SYSTEM = "You are a computation and visualization planner. Respond with exactly one line."

#: 一句"没查过"的提示 —— 必须与"查了但为空"区分开。
NO_QUERY_HINT = (
    "你还没有执行任何查询 —— 本轮只做了结构探索，没有调用 query_database。"
    "请立即用 query_database 执行一条具体的 SELECT（计数用 COUNT(*)，列表加行数上限），"
    "不要继续确认表结构。"
)
EMPTY_RESULT_HINT = (
    "查询执行了，但没有匹配的数据。请检查 WHERE 条件是否过严（例如编码值写错、"
    "时间范围不对），或换一个列/表来表达同一个意思；不要重复确认表结构。"
)

#: 关键词兜底（模型不可用或答非所问时用）。
_COMPUTE_KEYWORDS = ("平均", "率", "占比", "统计", "计算", "排名", "对比", "比率")
_CHART_KEYWORDS = ("图表", "图", "柱状图", "饼图", "折线图", "可视化", "趋势", "分布")


@dataclass(slots=True, frozen=True)
class Evaluation:
    """一轮结果的评估结论。"""

    satisfactory: bool
    reason: str
    #: 不充分时给下一轮的具体建议。
    hint: str = ""
    #: 这一轮是否真的执行过查询（供用例层判断是否要发重试事件）。
    ran_query: bool = True


@dataclass(slots=True, frozen=True)
class ComputationPlan:
    """是否需要计算 / 画图。"""

    needs_computation: bool = False
    strategy: str = ""
    needs_chart: bool = False
    chart_strategy: str = ""


def _total_rows(results: Sequence[QueryResult]) -> int:
    return sum(result.row_count for result in results)


def _summarize(results: Sequence[QueryResult], sample_rows: int = 3) -> list[str]:
    parts: list[str] = []
    for index, result in enumerate(results, 1):
        parts.append(
            f"查询{index}: {result.row_count}行, 列={list(result.columns)}, "
            f"样例={result.rows[:sample_rows]}"
        )
    return parts


async def evaluate_results(
    results: Sequence[QueryResult],
    question: str,
    *,
    llm: LLMClient,
    ran_any_query: bool,
) -> Evaluation:
    """判断这一轮的结果是否已经回答了问题。

    Args:
        results: 本轮累积的查询结果。
        question: 用户问题。
        llm: 模型客户端（非流式）。
        ran_any_query: 本次运行是否**真的**执行过查询 —— 这是区分
            "没查"与"查了为空"的唯一依据。
    """
    # ---- 分支一：这一轮压根没有查过（历史实现与"查了为空"混为一谈）----
    if not ran_any_query:
        return Evaluation(
            satisfactory=False,
            reason="本轮没有执行任何查询",
            hint=NO_QUERY_HINT,
            ran_query=False,
        )

    # ---- 分支二：查过，但结果为空 ----
    if not results or _total_rows(results) == 0:
        return Evaluation(
            satisfactory=False,
            reason="查询执行了，但没有返回数据",
            hint=EMPTY_RESULT_HINT,
            ran_query=True,
        )

    if not llm.available:
        return Evaluation(
            satisfactory=True,
            reason=f"共 {_total_rows(results)} 条结果",
            ran_query=True,
        )

    prompt = (
        "判断以下查询结果是否已经充分回答了用户问题。\n\n"
        "评估标准：\n"
        "1. 结果完整回答了用户问题（数量、统计、列表等都已提供）→ SATISFIED\n"
        "2. 结果为空、不完整、仅采样未聚合、或含糊其辞（如「无法确认」）→ RETRY\n"
        "3. 重试建议必须具体：指出缺少什么信息，建议查询哪些表/列，用什么条件\n\n"
        "核心原则：结果必须能直接回答用户问题才算充分，否则就重试。\n"
        "注意：查询已经执行过了（不是没查），所以重试建议应当针对查询口径，"
        "而不是要求再次确认表结构。\n\n"
        f"用户问题: {question}\n\n"
        "查询结果:\n" + "\n".join(_summarize(results)) + "\n\n"
        "只回复一行：SATISFIED 或 RETRY: <基于数据库内容的改进建议>"
    )

    try:
        text = (
            await llm.complete(
                _EVALUATOR_SYSTEM,
                [{"role": "user", "content": prompt}],
                max_tokens=_DECIDE_MAX_TOKENS,
                temperature=0.0,
            )
        ).strip()
    except LlmError as exc:
        logger.warning("结果评估调用失败，退回行数启发式: %s", exc.detail or exc.user_message)
        return Evaluation(
            satisfactory=True,
            reason=f"共 {_total_rows(results)} 条结果",
            ran_query=True,
        )

    upper = text.upper()
    if upper.startswith("SATISFIED"):
        return Evaluation(
            satisfactory=True,
            reason=f"共 {_total_rows(results)} 条结果",
            ran_query=True,
        )
    if upper.startswith("RETRY:"):
        hint = text[6:].strip() or EMPTY_RESULT_HINT
        return Evaluation(
            satisfactory=False,
            reason="模型判定结果不充分",
            hint=hint,
            ran_query=True,
        )

    # 答非所问：有数据就算充分（历史行为如此）。
    logger.info("评估器返回了无法解析的内容: %r", text[:120])
    return Evaluation(
        satisfactory=True,
        reason=f"共 {_total_rows(results)} 条结果",
        ran_query=True,
    )


async def plan_computation(
    question: str,
    results: Sequence[QueryResult],
    *,
    llm: LLMClient,
    ran_any_query: bool,
) -> ComputationPlan:
    """决定是否需要计算与画图。"""
    if not results or not ran_any_query:
        return ComputationPlan()

    summaries = _summarize(results, sample_rows=2)
    if not summaries:
        return ComputationPlan()

    if not llm.available:
        return _keyword_plan(question)

    prompt = (
        "判断以下查询结果是否需要进行数学计算和/或图表可视化。\n\n"
        f"用户问题: {question}\n\n"
        "查询结果概要:\n" + "\n".join(summaries) + "\n\n"
        "## 计算判断\n"
        "如果需要进行统计、聚合、排序、计算比率/百分比/平均值等，回复: COMPUTE: <简短思路>\n\n"
        "## 可视化判断\n"
        "如果数据满足以下任一条件，建议生成图表：\n"
        "- 有类别+数值的数据对（如各部门人数、各考试通过率）→ 柱状图或饼图\n"
        "- 有时间序列（日期/年份作为X轴，数值作为Y轴）→ 折线图\n"
        "- 有两个数值列 → 散点图\n"
        "- 有多行数值 → 直方图\n"
        "- 用户明确要求图表、趋势分析、可视化\n"
        "如果需要图表，回复: CHART: <图表类型和说明>\n\n"
        "如果既需要计算也需要图表，回复: COMPUTE+CHART: <计算思路> | <图表说明>\n\n"
        "如果都不需要，回复: SKIP\n\n"
        "只回复一行。"
    )

    try:
        text = (
            await llm.complete(
                _PLANNER_SYSTEM,
                [{"role": "user", "content": prompt}],
                max_tokens=_PLAN_MAX_TOKENS,
                temperature=0.0,
            )
        ).strip()
    except LlmError as exc:
        logger.warning("计算规划调用失败，退回关键词判定: %s", exc.detail or exc.user_message)
        return _keyword_plan(question)

    return _parse_plan(text, question)


def _parse_plan(text: str, question: str) -> ComputationPlan:
    upper = text.upper()
    if upper.startswith("COMPUTE+CHART:"):
        parts = text[15:].strip().split("|", 1)
        return ComputationPlan(
            needs_computation=True,
            strategy=parts[0].strip() if parts else "",
            needs_chart=True,
            chart_strategy=parts[1].strip() if len(parts) > 1 else "",
        )
    if upper.startswith("COMPUTE:"):
        return ComputationPlan(needs_computation=True, strategy=text[8:].strip())
    if upper.startswith("CHART:"):
        return ComputationPlan(needs_chart=True, chart_strategy=text[6:].strip())
    if upper.startswith("SKIP"):
        return ComputationPlan()
    logger.info("规划器返回了无法解析的内容: %r", text[:120])
    return _keyword_plan(question)


def _keyword_plan(question: str) -> ComputationPlan:
    return ComputationPlan(
        needs_computation=any(keyword in question for keyword in _COMPUTE_KEYWORDS),
        needs_chart=any(keyword in question for keyword in _CHART_KEYWORDS),
    )
