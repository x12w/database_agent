"""对话历史的整理与回答清洗。

两件历史遗留问题都收敛在这里：

* `_strip_sql_from_text` —— 回答里冒出 SQL 是提示词约束不够硬时的常见残留，
  历史实现用三条正则清洗。保留行为，但**不再**是唯一防线：提示词里也明确
  禁止输出 SQL，且结果渲染不把 SQL 交给回答 agent。
* 历史长度上限 —— 历史实现硬编码 20 条（`orchestrator.py:1738`）。
"""

from __future__ import annotations

import re

__all__ = [
    "MAX_HISTORY_MESSAGES",
    "build_messages",
    "strip_sql_from_text",
    "trim_history",
]

#: 会话历史保留的消息条数上限（一问一答算两条）。
MAX_HISTORY_MESSAGES = 20

_SQL_PREFIX_BLOCK = re.compile(
    r"\bSQL\s*(?:SELECT|WITH)\b.*?(?:LIMIT\s+\d+|\);)",
    re.IGNORECASE | re.DOTALL,
)
_SQL_STATEMENT = re.compile(
    r"\bSELECT\s+.*?\bFROM\b.*?(?:LIMIT\s+\d+|\);)",
    re.IGNORECASE | re.DOTALL,
)
_SQL_LINE = re.compile(
    r"^\s*(?:SQL|--)\s|"
    r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|WITH)\b|"
    r"^\s*(?:FROM|WHERE|JOIN|GROUP\s+BY|ORDER\s+BY|HAVING|UNION|LIMIT|OFFSET)\b|"
    r'^\s*"[^"]+"\."[^"]+"',
    re.IGNORECASE,
)
_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r" {2,}")


def build_messages(history: list[dict] | None, question: str, context: str = "") -> list[dict]:
    """拼出本次的用户消息（可带时间解析上下文）。

    带上下文时保留 `[系统时间参考]` 前缀 —— 历史实现如此，模型据此知道
    时间不是它要猜的东西。
    """
    messages: list[dict] = list(history or [])
    if context:
        messages.append({"role": "user", "content": f"{context}\n\n用户问题: {question}"})
    else:
        messages.append({"role": "user", "content": question})
    return messages


def strip_sql_from_text(text: str) -> str:
    """清掉回答里残留的 SQL 片段。"""
    if not text:
        return ""
    cleaned = _SQL_PREFIX_BLOCK.sub("", text)
    cleaned = _SQL_STATEMENT.sub("", cleaned)
    lines = [line for line in cleaned.split("\n") if not _SQL_LINE.match(line.strip())]
    result = "\n".join(lines)
    result = _BLANK_LINES.sub("\n\n", result)
    result = _SPACES.sub(" ", result)
    return result.strip()


def trim_history(history: list[dict]) -> list[dict]:
    """裁剪历史到上限（保留最近的）。"""
    if len(history) <= MAX_HISTORY_MESSAGES:
        return list(history)
    return history[-MAX_HISTORY_MESSAGES:]
