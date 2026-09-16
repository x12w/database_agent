"""时间表述的确定性预解析。

这一段必须留在模型之外：`[系统时间参考]` 由系统时钟算出、拼进用户消息，
模型无法忽略也无法篡改。历史实现（`orchestrator.py:58-99`）就是这么做的，
行为原样保留 —— 包括"只有问题里出现时间关键词时才注入"。

`now` 可注入是为了让测试拿到确定的日期，而不是让调用方改系统时间。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

__all__ = ["TEMPORAL_KEYWORDS", "TemporalContext", "resolve_temporal_context"]

#: 触发时间解析的关键词。
TEMPORAL_KEYWORDS: tuple[str, ...] = (
    "今年", "本年", "去年", "前年", "明年",
    "本月", "这个月", "上个月", "下个月",
    "本周", "这周", "上周", "下周",
    "今天", "昨天", "明天", "当前", "现在",
    "上半年", "下半年", "第一季度", "第二季度", "第三季度", "第四季度",
    "最近", "近期", "近三年", "近五年",
)


@dataclass(slots=True, frozen=True)
class TemporalContext:
    """解析结果。"""

    #: 要拼到用户消息前面的整段文本（没有时间表述时为空串）。
    text: str
    #: 命中的关键词。
    keywords: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.text)


def resolve_temporal_context(
    question: str, *, now: datetime.datetime | None = None
) -> TemporalContext:
    """把"去年""上个月"这类表述解析成具体年月。

    Returns:
        `TemporalContext`；问题里没有时间表述时 `text` 为空串。
    """
    if not question:
        return TemporalContext(text="", keywords=())

    matched = tuple(keyword for keyword in TEMPORAL_KEYWORDS if keyword in question)
    if not matched:
        return TemporalContext(text="", keywords=())

    moment = now or datetime.datetime.now()
    quarter = (moment.month - 1) // 3 + 1
    lines = [
        f"[系统时间参考] 当前时间是 {moment.year}年{moment.month}月{moment.day}日，"
        f"年份={moment.year}，月份={moment.month}，季度=Q{quarter}。"
        f"以下时间表述均基于此计算，不需要再搜索确认时间。",
    ]

    if "去年" in question:
        lines.append(f"「去年」= {moment.year - 1}年")
    if "今年" in question or "本年" in question:
        lines.append(f"「今年」= {moment.year}年")
    if "上半年" in question:
        lines.append(f"「上半年」= {moment.year}年1月至6月")
    if "下半年" in question:
        lines.append(f"「下半年」= {moment.year}年7月至12月")
    if "本月" in question or "这个月" in question:
        lines.append(f"「本月」= {moment.year}年{moment.month}月")
    if "上个月" in question:
        previous = moment.replace(day=1) - datetime.timedelta(days=1)
        lines.append(f"「上个月」= {previous.year}年{previous.month}月")
    if "第一季度" in question:
        lines.append(f"「第一季度」= {moment.year}年1月至3月")
    if "第二季度" in question:
        lines.append(f"「第二季度」= {moment.year}年4月至6月")
    if "第三季度" in question:
        lines.append(f"「第三季度」= {moment.year}年7月至9月")
    if "第四季度" in question:
        lines.append(f"「第四季度」= {moment.year}年10月至12月")

    return TemporalContext(text="\n".join(lines), keywords=matched)
