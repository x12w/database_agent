"""内部事件 → 线上事件的投影层，也是信息泄漏的**唯一闸门**。

`project()` 是 AgentEvent 变成 ChatEvent 的唯一途径。它按白名单从
`ToolFinished.payload` 里取标量字段（行数、耗时），其余一律丢弃 ——
原始 SQL、行数据、工具内部名不会进入线上事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dbchat.events.agent import (
    AdminQuestionRaised,
    AgentEvent,
    AgentFailed,
    AgentFinished,
    TextDelta,
    ThinkingDelta,
    ToolFinished,
    ToolStarted,
)
from dbchat.events.base import ActivityKind
from dbchat.events.chat import (
    ActivityFinished,
    ActivityStarted,
    AnswerDelta,
    QuestionAsked,
    RunFailed,
    ThoughtDelta,
)
from dbchat.domain.errors import DB_ERROR_MESSAGES, DbErrorKind

#: 工具内部名 → (动作类别, 展示用目标描述函数)。
#:
#: 这张表是工具名唯一的出站映射：内部名到这里为止，浏览器只看到 label。
TOOL_VIEW: dict[str, tuple[ActivityKind, str]] = {
    "query_database": (ActivityKind.QUERY, ""),
    "get_schema_summary": (ActivityKind.SCHEMA, ""),
    "list_tables": (ActivityKind.SCHEMA, ""),
    "describe_table": (ActivityKind.SCHEMA, ""),
    "get_table_detail": (ActivityKind.SCHEMA, ""),
    "sample_data": (ActivityKind.SCHEMA, ""),
    "analyze_column": (ActivityKind.SCHEMA, ""),
    "find_related_tables": (ActivityKind.SCHEMA, ""),
    "find_join_path": (ActivityKind.SCHEMA, ""),
    "get_suggested_questions": (ActivityKind.SCHEMA, ""),
    "search_documentation": (ActivityKind.DOCS, ""),
    "search_web": (ActivityKind.SEARCH, ""),
    "ask_admin": (ActivityKind.ADMIN_ASK, ""),
    "run_python": (ActivityKind.COMPUTE, ""),
}

_FALLBACK_VIEW = (ActivityKind.COMPUTE, "")

#: payload 里可以出现在线上事件中的标量字段白名单。
_PAYLOAD_SCALARS = ("row_count", "execution_ms", "truncated")


@dataclass(slots=True)
class RunView:
    """一次运行的投影上下文（序号、run_id、时间戳来源）。"""

    run_id: str
    seq: int = 0
    now: float = 0.0
    #: 是否允许把 SQL 文本发给前端（默认关闭）。
    expose_sql: bool = False
    started_at: dict[str, float] = field(default_factory=dict)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


def describe_target(tool: str, tool_input: dict) -> str | None:
    """从工具入参里提炼一个可展示的目标名。

    只取表名、文档查询词这类用户可理解的名词，绝不返回 SQL。
    """
    for key in ("table_name", "table", "target_table", "source_table", "column_name", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            return text if len(text) <= 60 else text[:57] + "..."
    return None


def _error_code_for(agent_error_code: str) -> str:
    return agent_error_code or "internal"


def project(event: AgentEvent, view: RunView) -> object | None:
    """把一个内部事件投影成线上事件；无需展示时返回 None。"""
    ts = view.now
    seq = view.next_seq()

    match event:
        case ThinkingDelta(text=text):
            return ThoughtDelta(seq=seq, ts=ts, run_id=view.run_id, text=text)

        case TextDelta(text=text):
            return AnswerDelta(seq=seq, ts=ts, run_id=view.run_id, text=text)

        case ToolStarted(invocation_id=iid, tool=tool, tool_input=inp):
            kind, _ = TOOL_VIEW.get(tool, _FALLBACK_VIEW)
            from dbchat.events.base import ACTIVITY_LABELS

            return ActivityStarted(
                seq=seq,
                ts=ts,
                run_id=view.run_id,
                activity_id=iid,
                kind=kind,
                label=ACTIVITY_LABELS[kind],
                target=describe_target(tool, inp),
            )

        case ToolFinished(invocation_id=iid, ok=ok, summary=summary, payload=payload):
            # 只按白名单取标量 —— payload 的其余部分（SQL、行数据）到此为止。
            scalars = {k: payload[k] for k in _PAYLOAD_SCALARS if k in payload}
            sql_preview = payload.get("sql") if view.expose_sql else None
            return ActivityFinished(
                seq=seq,
                ts=ts,
                run_id=view.run_id,
                activity_id=iid,
                ok=ok,
                summary=summary,
                row_count=scalars.get("row_count"),
                elapsed_ms=scalars.get("execution_ms"),
                sql_preview=sql_preview if isinstance(sql_preview, str) else None,
                error_detail=payload.get("error_detail") if isinstance(payload.get("error_detail"), str) else None,
                artifact_ids=[a.artifact_id for a in payload.get("artifacts", ())],
            )

        case AdminQuestionRaised(
            question_id=qid, table=table, column=column, question=_q, samples=samples
        ):
            return QuestionAsked(
                seq=seq,
                ts=ts,
                run_id=view.run_id,
                question_id=qid,
                label="需要你确认一列的取值含义",
                table=table,
                column=column,
                samples=list(samples)[:8],
            )

        case AgentFailed(code=code, message=message, detail=detail):
            from dbchat.events.base import RETRYABLE_ERRORS, ErrorCode

            try:
                ec = ErrorCode(code)
            except ValueError:
                ec = ErrorCode.INTERNAL
            return RunFailed(
                seq=seq,
                ts=ts,
                run_id=view.run_id,
                code=ec,
                message=message or DB_ERROR_MESSAGES[DbErrorKind.UNKNOWN],
                retryable=ec in RETRYABLE_ERRORS,
                error_detail=detail or None,
            )

        case AgentFinished():
            # 内部信号，不直接产生线上事件（由用例层汇总成 run.finished）。
            return None

    return None


__all__ = ["RunView", "TOOL_VIEW", "describe_target", "project"]
