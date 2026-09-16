"""主动探索用例（管理面板）。

对应历史实现的 `orchestrator.run_proactive_exploration`（`:2105-2486`）。
这不是用户提问触发的，而是管理员主动发起的一次全库扫描：找出所有"含义不明的
编码列"，逐列向管理员提问，逐步把知识库补全。

产出**探索通道**的事件（`dbchat.events.explore` 的 5 种）。这条通道的读者是
管理员，表名、列名、样本值、以及 agent 的推测都是必要信息 —— 与聊天通道
（读者是普通用户）刻意分开。

保留的行为：

* 候选列启发式（见 `agent.candidates`）—— 短 ASCII 码、小范围整数码、
  枚举类型，以及"自明列不提问"的排除清单；
* `ask_admin` 的去重语义 —— 已经问过（待答或已答）的列不再重复提问，
  这条完全落在 `storage.clarifications` 上，本模块不另加判断；
* 每轮最多处理 20 张表（历史实现的封顶，避免一次扫全库把管理员淹掉）。

丢掉的行为：历史实现用 `db.cursor()` 直接拼 `SELECT ... LIMIT 3` 取上下文
（同步驱动、按库类型无分支但也没做标识符转义），这里换成
`SqlExecutor` + `Dialect.quote/limit_clause`。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from dbchat.agent.candidates import Candidate, find_candidates
from dbchat.config import Settings, get_settings
from dbchat.db import get_connection_manager
from dbchat.db.executor import SqlExecutor
from dbchat.domain.errors import DbChatError
from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.query import BoundSQL
from dbchat.domain.schema import CatalogScope, SchemaSnapshot, TableMeta
from dbchat.events.base import ErrorCode
from dbchat.events.explore import (
    ExploreFailed,
    ExploreFinished,
    ExploreProgress,
    ExploreQuestion,
    ExploreStarted,
)
from dbchat.llm.client import LLMClient, get_llm_client
from dbchat.storage.clarifications import ClarificationRepository, get_clarification_repository
from dbchat.tools.docs import AskAdminParams, ask_admin
from dbchat.tools.registry import ToolContext
from dbchat.usecases.contracts import RunContext

logger = logging.getLogger(__name__)

__all__ = ["ProactiveExplorer", "build_explore_use_case"]

#: 单次探索最多处理的表数（历史实现的封顶）。
MAX_TABLES_PER_RUN = 20
#: 每张表最多带几列上下文数据给管理员看。
_CONTEXT_ROWS = 3
#: 每张表最多取几个候选列做上下文查询。
_CONTEXT_COLUMNS = 3
#: 上下文行的字符上限。
_CONTEXT_CELL_CHARS = 30


class ProactiveExplorer:
    """主动探索（实现 `usecases.contracts.ExploreUseCase`）。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        clarifications: ClarificationRepository | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm = llm or get_llm_client()
        self._clarifications = clarifications or get_clarification_repository()

    # ------------------------------------------------------------ 入口

    def run(
        self, *, profile: DatabaseProfile, scope: CatalogScope
    ) -> AsyncIterator[object]:
        """跑一次全库探索，产出 `explore.*` 事件。"""
        return self._run(profile=profile, scope=scope)

    # ------------------------------------------------------------ 主体

    async def _run(
        self, *, profile: DatabaseProfile, scope: CatalogScope
    ) -> AsyncIterator[object]:
        run_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        seq = _Seq()

        try:
            snapshot, candidates = await self._collect(profile, scope)
        except DbChatError as exc:
            logger.warning("主动探索失败（%s）: %s", exc.code, exc.detail or exc.user_message)
            yield ExploreFailed(
                seq=seq.next(),
                ts=time.time(),
                run_id=run_id,
                code=_error_code(exc.code),
                message=exc.user_message,
                error_detail=exc.detail or None,
            )
            return

        yield ExploreStarted(
            seq=seq.next(),
            ts=time.time(),
            run_id=run_id,
            profile=_profile_view(profile),
            candidates=len(candidates),
        )

        if not candidates:
            async for event in self._nothing_to_ask(profile, run_id, seq, started):
                yield event
            return

        by_table: dict[str, list[Candidate]] = {}
        for candidate in candidates:
            by_table.setdefault(candidate.table, []).append(candidate)

        questions_asked = 0
        tables_scanned = 0
        total = len(candidates)
        done = 0

        for table_name, columns in list(by_table.items())[:MAX_TABLES_PER_RUN]:
            tables_scanned += 1
            meta = _lookup(snapshot, table_name)
            if meta is None:
                continue

            yield ExploreProgress(
                seq=seq.next(),
                ts=time.time(),
                run_id=run_id,
                done=done,
                total=total,
                current_table=table_name,
                note=f"正在分析 {table_name}（{len(columns)} 个编码列）",
            )

            context = await self._context(profile, meta, columns)

            for candidate in columns:
                done += 1
                # 去重语义完全在仓储层：问过的列（待答或已答）直接跳过。
                known = await self._clarifications.aget_known_columns(profile.id)
                if candidate.key in known:
                    continue

                guess, question = await self._formulate(candidate, context)
                yield await self._ask(profile, candidate, question, guess, context, run_id, seq)
                questions_asked += 1

                yield ExploreProgress(
                    seq=seq.next(),
                    ts=time.time(),
                    run_id=run_id,
                    done=done,
                    total=total,
                    current_table=table_name,
                    note=f"已提问 {questions_asked} 个",
                )

        pending = await self._clarifications.aget_pending_count(profile.id)
        yield ExploreFinished(
            seq=seq.next(),
            ts=time.time(),
            run_id=run_id,
            candidates_found=len(candidates),
            questions_asked=questions_asked,
            pending_count=pending,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        logger.info(
            "主动探索完成：扫描 %d 张表，候选 %d 列，提问 %d 个，待确认 %d 个",
            tables_scanned,
            len(candidates),
            questions_asked,
            pending,
        )

    # ------------------------------------------------------------ 阶段

    async def _collect(
        self, profile: DatabaseProfile, scope: CatalogScope
    ) -> tuple[SchemaSnapshot, list[Candidate]]:
        """拿结构快照 + 候选列。"""
        snapshot = await self._snapshot(profile, scope)
        known = await self._clarifications.aget_known_columns(profile.id)
        tables = {
            name: meta
            for name, meta in snapshot.tables.items()
            if scope.allows(name)
        }
        return snapshot, find_candidates(tables, known_columns=known)

    async def _snapshot(
        self, profile: DatabaseProfile, scope: CatalogScope
    ) -> SchemaSnapshot:
        from dbchat.db import build_snapshot

        async with get_connection_manager().acquire(profile) as lease:
            executor = SqlExecutor(lease, self._settings)
            return await build_snapshot(executor, scope)

    async def _context(
        self, profile: DatabaseProfile, meta: TableMeta, columns: list[Candidate]
    ) -> list[dict[str, Any]]:
        """取几行"候选列 + 名称列"的上下文，供管理员判断编码含义。"""
        target = [candidate.column for candidate in columns[:_CONTEXT_COLUMNS]]
        companions = [
            column.name
            for column in meta.columns
            if column.name not in target
            and column.name.lower().endswith(("name", "_no", "_code"))
        ][:_CONTEXT_COLUMNS]
        selected = [*target, *companions]
        if not selected:
            return []

        dialect = _dialect(profile)
        sql = (
            "SELECT "
            + ", ".join(dialect.quote(name) for name in selected)
            + f" FROM {dialect.quote(meta.name)} "
            + dialect.limit_clause(_CONTEXT_ROWS)
        )
        try:
            async with get_connection_manager().acquire(profile) as lease:
                executor = SqlExecutor(lease, self._settings)
                result = await executor.fetch_all(BoundSQL(sql))
        except Exception as exc:  # noqa: BLE001 - 上下文拿不到不影响提问
            logger.info(
                "取 %s 的上下文数据失败: %s: %s", meta.name, type(exc).__name__, exc
            )
            return []
        return [dict(row) for row in result]

    async def _formulate(
        self, candidate: Candidate, context: list[dict[str, Any]]
    ) -> tuple[str, str]:
        """让模型给出推测与提问；模型不可用时退回模板问题。"""
        fallback = (
            f"`{candidate.table}` 表的 `{candidate.column}` 列包含值 "
            f"[{', '.join(candidate.samples[:5])}]，请问这些值分别代表什么含义？"
        )
        if not self._llm.available:
            return "", fallback

        prompt = [
            f"你在探索数据库表 `{candidate.table}`，发现了一个含义不明的列。\n",
            f"**列名**: `{candidate.column}`",
            f"**类型**: {candidate.column_type}",
            f"**样本值**: {', '.join(candidate.samples[:5])}\n",
        ]
        if context:
            prompt.append("**相关数据上下文**（该列与其他列一起展示）:")
            prompt.append(_render_context(context))
            prompt.append("")
        prompt.append(
            "请分析这些数据，尝试**推测**这个列的业务含义。然后输出两部分：\n\n"
            "## 推测\n"
            "基于上下文给出你的推测：这些编码值可能代表什么？给出具体映射。\n"
            "例如：'01=在职, 02=离职' 或 'A=优秀, B=良好, C=及格, D=不及格'\n\n"
            "## 问题\n"
            "用一句话向管理员确认，包含你的推测。"
        )

        try:
            text = (
                await self._llm.complete(
                    "你是数据库分析专家。分析数据上下文，推测编码列含义，用中文输出。",
                    [{"role": "user", "content": "\n".join(prompt)}],
                )
            ).strip()
        except Exception as exc:  # noqa: BLE001 - 生成问题失败就用模板问题
            logger.info(
                "生成管理员问题失败 (%s.%s): %s: %s",
                candidate.table,
                candidate.column,
                type(exc).__name__,
                exc,
            )
            return "", fallback

        if "## 推测" in text and "## 问题" in text:
            guess, _, question = text.partition("## 问题")
            return guess.replace("## 推测", "").strip(), question.strip() or fallback
        return text[:500], text or fallback

    async def _ask(
        self,
        profile: DatabaseProfile,
        candidate: Candidate,
        question: str,
        guess: str,
        context: list[dict[str, Any]],
        run_id: str,
        seq: _Seq,
    ) -> ExploreQuestion:
        """提交给管理员（去重由 `ask_admin` → 仓储决定）。"""
        ctx = ToolContext(
            ctx=RunContext(
                run_id=run_id,
                profile=profile,
                scope=CatalogScope(),
                conversation_id="",
            ),
            settings=self._settings,
            clarifications=self._clarifications,
        )
        outcome = await ask_admin(
            AskAdminParams(
                table_name=candidate.table,
                column_name=candidate.column,
                question=question,
                samples=candidate.samples,
            ),
            ctx,
        )
        payload = outcome.payload
        existing = payload.get("already_answered")
        return ExploreQuestion(
            seq=seq.next(),
            ts=time.time(),
            run_id=run_id,
            question_id=str(payload.get("question_id", "")),
            table=payload.get("table_name", candidate.table),
            column=payload.get("column_name", candidate.column),
            question=question,
            agent_guess=guess,
            samples=candidate.samples[:8],
            context_columns=list(context[0].keys()) if context else [],
            context_rows=[
                [
                    str(value)[:_CONTEXT_CELL_CHARS]
                    for value in row.values()
                ]
                for row in context[:_CONTEXT_ROWS]
            ],
            existing_answer=outcome.content if existing else None,
        )

    async def _nothing_to_ask(
        self, profile: DatabaseProfile, run_id: str, seq: _Seq, started: float
    ) -> AsyncIterator[object]:
        """没有新候选：把待确认的问题重新发一遍（前端据此渲染列表）。"""
        pending = await self._clarifications.aget_pending(profile.id)
        for entry in pending:
            yield ExploreQuestion(
                seq=seq.next(),
                ts=time.time(),
                run_id=run_id,
                question_id=entry.question_id,
                table=entry.table_name,
                column=entry.column_name,
                question=entry.question,
                agent_guess=entry.agent_guess,
                samples=[str(value) for value in entry.sample_values],
            )
        yield ExploreFinished(
            seq=seq.next(),
            ts=time.time(),
            run_id=run_id,
            candidates_found=0,
            questions_asked=0,
            pending_count=len(pending),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


# ------------------------------------------------------------------ 辅助


class _Seq:
    """探索通道的事件序号。"""

    def __init__(self) -> None:
        self._value = 0

    def next(self) -> int:
        self._value += 1
        return self._value


def _profile_view(profile: DatabaseProfile) -> Any:
    from dbchat.events.base import ProfileView

    return ProfileView(
        id=profile.id,
        name=profile.display_name,
        dialect_label=_dialect(profile).display_name,
    )


def _lookup(snapshot: SchemaSnapshot, table: str) -> TableMeta | None:
    meta = snapshot.tables.get(table)
    if meta is not None:
        return meta
    lowered = table.lower()
    for name, candidate in snapshot.tables.items():
        if name.lower() == lowered:
            return candidate
    return None


def _dialect(profile: DatabaseProfile):
    """当前连接的方言实例（引用字符与行数限制都由它决定）。"""
    from dbchat.db.dialects import get_dialect

    return get_dialect(profile.kind)


def _render_context(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    lines = ["```", " | ".join(headers), " | ".join(["---"] * len(headers))]
    for row in rows[:_CONTEXT_ROWS]:
        lines.append(
            " | ".join(str(row.get(header, ""))[:_CONTEXT_CELL_CHARS] for header in headers)
        )
    lines.append("```")
    return "\n".join(lines)


def _error_code(code: str) -> ErrorCode:
    mapping = {
        "db": ErrorCode.DB_UNREACHABLE,
        "llm": ErrorCode.LLM_FAILED,
        "llm_unavailable": ErrorCode.LLM_UNAVAILABLE,
        "config": ErrorCode.INTERNAL,
        "validation": ErrorCode.VALIDATION,
        "tool": ErrorCode.INTERNAL,
    }
    return mapping.get(code, ErrorCode.INTERNAL)


def build_explore_use_case(ctx: Any) -> ProactiveExplorer:
    """按 `di.AppContext` 构造探索用例（API 层惰性导入的工厂名）。"""
    return ProactiveExplorer(
        settings=ctx.settings,
        llm=ctx.llm,
        clarifications=ctx.clarifications,
    )
