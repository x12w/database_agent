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

**缺陷修复（评估不认"没有"这个答案）**：分支二曾经无条件要求重试，于是
"有没有叫张三丰的员工参加过考试？"这种**是非问句**被烧掉 4 轮 / 173 秒 ——
一条正确靶向的查询返回 0 行，0 行本身就是答案。现在先判问题形状
（`is_boolean_question`）：是非问句的空结果按"没有匹配数据"直接收下；
计数/列表/统计类问题的空结果仍然重试（那是真的没查到）。

**缺陷修复（重试理由自相矛盾）**：评估器只认同一个前缀 ——
`deciders.py:180` 判断 `startswith("RETRY:")` 后把剩余文本原样当建议，
于是 `RETRY: 查询已返回各考试平均分`（`chart` 用例第 2 轮的原文）也照单
全收，白烧一整轮（约 50 秒）。现在改为请模型回一个 JSON 对象
（`{"satisfied", "reason", "hint"}`），解析容忍三种形状（完整 JSON、
被 token 上限截断的 JSON、历史前缀协议），并在"理由是成功陈述"或
"建议里没有任何可执行改动"时按已充分处理。

**注意 token 预算**：改问 JSON 之后 `_DECIDE_MAX_TOKENS` 从 200 提到 400 ——
中文 reason + hint 顶满上限时 JSON 会被截断，而截断的回复一旦落到
"答非所问即为充分"，真正的重试判定就被静默丢掉了（实测已贴上限）。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from dbchat.domain.errors import LlmError
from dbchat.domain.query import QueryResult
from dbchat.llm.client import LLMClient

__all__ = [
    "ComputationPlan",
    "Evaluation",
    "evaluate_results",
    "is_boolean_question",
    "plan_computation",
]

logger = logging.getLogger(__name__)

#: 评估与规划只需要一行结论。
#:
#: 评估器改问 JSON 后 200 不够：中文 reason + hint 很容易顶满，
#: 截断的 JSON 解析不出来，就会被当成"答非所问 → 有数据即充分"，
#: 于是**真正的重试判定被静默丢掉**。实测同一问题 200 tokens 时
#: 回复已贴着上限。留一倍余量。
_DECIDE_MAX_TOKENS = 400
_PLAN_MAX_TOKENS = 150

_EVALUATOR_SYSTEM = (
    "You are a query result evaluator. Reply with one JSON object and nothing else."
)
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
#: 是非问句的空结果 —— 这是**答案**，不是失败。措辞必须让用户看得懂。
NO_MATCH_REASON = "没有匹配的数据 —— 「有没有」这类是非问题的答案就是：没有"

#: 关键词兜底（模型不可用或答非所问时用）。
_COMPUTE_KEYWORDS = ("平均", "率", "占比", "统计", "计算", "排名", "对比", "比率")
_CHART_KEYWORDS = ("图表", "图", "柱状图", "饼图", "折线图", "可视化", "趋势", "分布")

#: 是非问句的标志词（词汇取自 `app/agent/router.py:95` 的旧表 + 常见变体）。
_BOOLEAN_PATTERNS: tuple[str, ...] = (
    "有没有", "有没", "是否有", "是否", "是不是", "存在吗", "有吗",
)
#: 句尾的"吗"也是是非问句的强信号（"张三丰参加过考试吗？"）。
_QUESTION_TAIL = " \t\r\n？?。.！!"
#: 出现这些词就不是是非问句 —— "能帮我查一下有多少人吗？"是计数问题，
#: 0 行说明真没查到，不能因为句尾有个"吗"就收下。
_COUNT_PATTERNS: tuple[str, ...] = (
    "多少", "几个", "几名", "几张", "几条", "几号", "数量", "总数",
    "统计", "列出", "分别", "名单", "排名", "排序", "平均", "占比",
)

#: 重试建议里出现这些词，说明它给出了**可执行的改动**。
_DIRECTIVE_MARKERS: tuple[str, ...] = (
    "请", "建议", "需要", "需", "应该", "应", "必须", "改用", "换成", "换用",
    "改为", "替换", "尝试", "添加", "加上", "增加", "补充", "去掉", "删除",
    "过滤", "筛选", "限定", "关联", "联查", "使用", "针对", "重新", "检查",
    "核对", "明确", "区分", "按", "条件", "限制", "精确",
    "join", "where", "count", "group", "limit",
)
#: "已经成功了"式的措辞 —— 这种话不构成重试理由。
_SUCCESS_MARKERS: tuple[str, ...] = (
    "已返回", "已经返回", "已成功", "已查到", "已获得", "已完成", "已经完成",
    "已满足", "无需重试", "不需要重试", "已经得到", "已提供", "已充分",
    "已经回答", "已能回答", "结果已", "查询已返回",
)


def is_boolean_question(question: str) -> bool:
    """是否是一个"是/否"型（存在性）问题 —— 0 行就是答案。

    与 `classifier.is_existence_question` 的区别：后者把"哪些/谁"这类
    **列举**也算进去（那是分类用的，宽松无妨）。评估分支要收下空结果，
    必须更窄 —— "哪些员工没参加考试？"返回 0 行时更有可能是查错了。

    计数/统计类问题即使带疑问词尾（"有多少人吗？"）也不算是非问句。
    """
    text = (question or "").strip()
    if not text:
        return False
    if any(pattern in text for pattern in _COUNT_PATTERNS):
        return False
    if any(pattern in text for pattern in _BOOLEAN_PATTERNS):
        return True
    return text.rstrip(_QUESTION_TAIL).endswith("吗")


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
        # 是非问句的 0 行**就是答案**（"有没有叫张三丰的员工参加过考试？"）。
        # 计数/列表/统计类问题不走这条：它们返回 0 行说明口径错了，必须重试。
        if is_boolean_question(question):
            logger.info("是非问句返回 0 行，按「没有匹配数据」收下: %s", question[:60])
            return Evaluation(
                satisfactory=True,
                reason=NO_MATCH_REASON,
                ran_query=True,
            )
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

    boolean = is_boolean_question(question)
    prompt = (
        "判断以下查询结果是否已经充分回答了用户问题。\n\n"
        "评估标准：\n"
        "1. 结果完整回答了用户问题（数量、统计、列表等都已提供）→ satisfied=true\n"
        "2. 结果为空、不完整、仅采样未聚合、或含糊其辞（如「无法确认」）→ satisfied=false\n"
        "3. satisfied=false 时，hint 必须给出具体改动：缺少什么信息，"
        "查哪些表/列，用什么条件；**只描述现状、没有给出改动的不是 hint**\n\n"
        "核心原则：结果必须能直接回答用户问题才算充分，否则就重试。\n"
        "注意：查询已经执行过了（不是没查），所以重试建议应当针对查询口径，"
        "而不是要求再次确认表结构。\n\n"
        f"用户问题: {question}\n"
        f"问题类型: {'是非/存在性 —— 答案可以是「没有」' if boolean else '数据/统计'}\n"
    )
    if boolean:
        prompt += (
            "**这是是非问题**：只要已经有一条查询**针对该实体**执行过"
            "（例如带 WHERE 条件的过滤查询），那么它返回 0 行就已经回答了问题 —— "
            "结论是「没有」，应当 satisfied=true，不要因为「没找到」而要求重试。\n"
            "只有在该实体根本没有被查过时才 satisfied=false。\n"
        )
    prompt += (
        "\n查询结果:\n" + "\n".join(_summarize(results)) + "\n\n"
        "只回复一个 JSON 对象，不要输出其他任何内容（reason 不超过 30 字，hint 不超过 60 字）：\n"
        '{"satisfied": true|false, "reason": "<一句话结论>", '
        '"hint": "<satisfied=false 时的具体改进建议；true 时留空>"}'
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

    verdict = _parse_verdict(text)
    if verdict is None:
        # 答非所问：有数据就算充分（历史行为如此）。
        logger.info("评估器返回了无法解析的内容: %r", text[:120])
        return Evaluation(
            satisfactory=True,
            reason=f"共 {_total_rows(results)} 条结果",
            ran_query=True,
        )

    satisfied, model_reason, hint = verdict
    if satisfied:
        return Evaluation(
            satisfactory=True,
            reason=model_reason or f"共 {_total_rows(results)} 条结果",
            ran_query=True,
        )

    # ---- 缺陷修复 B：重试判定必须与它自己的理由一致 ----
    # `RETRY: 查询已返回各考试平均分` 这种"重试前缀 + 成功陈述"的回复，
    # 照单全收会白烧一整轮；理由是"已经查到了"或没给出任何改动时按已充分处理。
    if _reads_as_success(hint) or not _retry_hint_is_actionable(hint):
        logger.info("评估器给了 RETRY 但理由不可执行（%r），按已充分处理", hint[:120])
        return Evaluation(
            satisfactory=True,
            reason=f"共 {_total_rows(results)} 条结果",
            ran_query=True,
        )
    return Evaluation(
        satisfactory=False,
        reason=model_reason or "模型判定结果不充分",
        hint=hint,
        ran_query=True,
    )


def _parse_verdict(text: str) -> tuple[bool, str, str] | None:
    """把评估器的回复解析成 `(satisfied, reason, hint)`；解析不了返回 None。

    容忍三种形状（小模型三种都会给）：

    1. JSON：`{"satisfied": true|false, "reason": "...", "hint": "..."}` —— 首选；
    2. 被 token 上限截断的 JSON —— 按字段扫描取出结论（末字段的收尾引号
       缺失也算）。截断不等于"答非所问"，丢掉真正的重试判定会静默少查一轮；
    3. 历史前缀协议：`SATISFIED` / `RETRY: <建议>` —— 兼容旧提示词与桩客户端。

    判定不再挂在某一个前缀上：前缀只写 `RETRY`（无冒号）、大写小写、
    或者把 JSON 包在解释文字里，都能解析出来。
    """
    payload = _first_json_object(text)
    if payload is not None:
        satisfied = _as_bool(payload.get("satisfied"))
        if satisfied is not None:
            return (
                satisfied,
                str(payload.get("reason") or "").strip(),
                str(payload.get("hint") or "").strip(),
            )

    scanned = _scan_verdict_fields(text)
    if scanned is not None:
        logger.info("评估器回复的 JSON 不完整，按字段扫描取值")
        return scanned

    upper = text.upper().lstrip()
    if upper.startswith("SATISFIED"):
        return True, "", ""
    if upper.startswith("RETRY"):
        return False, "", text.lstrip()[5:].lstrip(":： \t").strip()
    return None


#: 截断回复里的字段扫描。字符串字段的收尾引号**可选** —— 被截断的正是
#: 最后那个字段（"hint": "请按部门分组 到这里就没了）。
_SATISFIED_FIELD = re.compile(r'"satisfied"\s*:\s*(true|false)', re.IGNORECASE)
_TEXT_FIELD = re.compile(r'"(?:reason|hint)"\s*:\s*"([^"]*)"?')


def _scan_verdict_fields(text: str) -> tuple[bool, str, str] | None:
    """从被截断的 JSON 里抠结论 —— 拿不到 `satisfied` 就返回 None。"""
    match = _SATISFIED_FIELD.search(text)
    if match is None:
        return None
    satisfied = match.group(1).lower() == "true"
    fields = _TEXT_FIELD.findall(text)
    reason = fields[0] if fields else ""
    hint = fields[1] if len(fields) > 1 else ""
    return satisfied, reason.strip(), hint.strip()


def _first_json_object(text: str) -> dict | None:
    """从回复里抠出第一个 JSON 对象（允许前后有解释文字）。"""
    for pattern in (r"\{[^{}]*\}", r"\{.*\}"):
        match = re.search(pattern, text, re.DOTALL)
        if match is None:
            continue
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _as_bool(value: object) -> bool | None:
    """宽松地把模型给的布尔值解析出来。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "y", "1", "是", "充分", "satisfied"):
            return True
        if lowered in ("false", "no", "n", "0", "否", "不充分", "retry"):
            return False
    return None


def _reads_as_success(hint: str) -> bool:
    """重试理由是不是在陈述"已经查到了"。"""
    return any(marker in hint for marker in _SUCCESS_MARKERS)


def _retry_hint_is_actionable(hint: str) -> bool:
    """重试理由里是否含有**可执行的改动**。

    空建议、纯现状描述（"查询已返回各考试平均分"）都不算 —— 这类回复
    只会让下一轮重复同一件事。
    """
    text = hint.strip()
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _DIRECTIVE_MARKERS)


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
