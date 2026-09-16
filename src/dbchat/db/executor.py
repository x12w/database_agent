"""查询执行门面 —— 校验 -> 执行 -> 截断 -> `QueryResult`。

用户 SQL 与内省 SQL 走同一条执行路径（都是 `BoundSQL`）：

* `query()` —— 用户/LLM 生成的 SQL：先过 `guard.inspect`（只读、限流、
  表列存在性），再执行，最后按 `settings.query_row_limit` 截断。
* `fetch_all()` —— 内省 SQL：方言自己生成的语句，已经是安全的、
  有界的，因此不重复校验，只走执行。

限流策略：向数据库要 `limit + 1` 行。多要的这 1 行是"是否被截断"的探针 ——
如果只按上限取，就永远分不清"恰好这么多行"和"其实更多但被截断了"。
这一行不会返回给调用方。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Protocol, TypeVar

import anyio

from dbchat.config import Settings, get_settings
from dbchat.db.connection import ConnectionLease
from dbchat.db.dialects.base import Dialect
from dbchat.db.guard import GuardResult, inspect
from dbchat.domain.errors import DB_ERROR_MESSAGES, DbError, ValidationError
from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.query import BoundSQL, QueryResult
from dbchat.domain.schema import CatalogScope, SchemaSnapshot

__all__ = ["SqlExecutor", "SqlRunner", "run_query"]

logger = logging.getLogger(__name__)

#: 探针行数：多取一行用于判断"结果被截断"。
_TRUNCATION_PROBE = 1

_T = TypeVar("_T")


class SqlRunner(Protocol):
    """内省层需要的最小执行接口。"""

    @property
    def dialect(self) -> Dialect: ...

    @property
    def profile(self) -> DatabaseProfile: ...

    async def fetch_all(self, sql: BoundSQL) -> list[Mapping[str, object]]: ...


class SqlExecutor:
    """绑定在一条连接租约上的查询门面。"""

    def __init__(self, lease: ConnectionLease, settings: Settings | None = None) -> None:
        self._lease = lease
        self._settings = settings or get_settings()

    # ------------------------------------------------------------ 属性

    @property
    def dialect(self) -> Dialect:
        return self._lease.dialect

    @property
    def profile(self) -> DatabaseProfile:
        return self._lease.profile

    @property
    def row_limit(self) -> int:
        return self._settings.query_row_limit

    # -------------------------------------------------------- 内省路径

    async def fetch_all(self, sql: BoundSQL) -> list[Mapping[str, object]]:
        """执行方言生成的 SQL（不校验、不截断）。

        Raises:
            DbError: 驱动报错，按方言归类。
        """
        await self._ensure_statement_timeout()

        def _run() -> list[Mapping[str, object]]:
            with self._lease.cursor() as cursor:
                cursor.execute(sql.sql, sql.params)
                if cursor.description is None:
                    return []
                return list(cursor.fetchall())

        return await self._in_thread(_run)

    # -------------------------------------------------------- 用户路径

    async def query(
        self,
        sql: str,
        *,
        snapshot: SchemaSnapshot | None = None,
        scope: CatalogScope | None = None,
    ) -> QueryResult:
        """校验并执行一条只读 SQL。

        Raises:
            ValidationError: 校验未通过（语法、只读性、表列存在性、表筛选）。
            DbError: 执行失败，按 `Dialect.classify_error` 分类。
        """
        limit = self.row_limit
        outcome = inspect(
            self.dialect,
            sql,
            snapshot=snapshot,
            scope=scope,
            # 多要一行作为截断探针。
            row_limit=limit + _TRUNCATION_PROBE,
        )
        if not outcome.ok:
            raise self._rejection(outcome)

        result = await self._execute(outcome.sql, limit)
        result.notices = list(outcome.warnings)
        if outcome.limited:
            result.notices.append(
                f"为控制返回体量，已自动补上行数上限 {self.dialect.limit_clause(limit)}"
            )
        return result

    def _rejection(self, outcome: GuardResult) -> ValidationError:
        detail = outcome.error_text()
        logger.info("SQL 校验未通过: %s | SQL: %s", detail, outcome.sql[:300])
        return ValidationError(
            f"生成的查询无法执行：{detail}",
            detail=detail,
        )

    async def _execute(self, sql: str, limit: int) -> QueryResult:
        await self._ensure_statement_timeout()
        started = time.perf_counter()

        def _run() -> tuple[list[str], list[Mapping[str, object]]]:
            with self._lease.cursor() as cursor:
                cursor.execute(sql)
                if cursor.description is None:
                    return [], []
                return cursor.column_names(), list(cursor.fetchall())

        try:
            columns, rows = await self._in_thread(_run)
        except DbError as exc:
            raise DbError(
                exc.kind,
                detail=exc.detail,
                user_message=f"查询执行失败：{exc.user_message}",
                cause=exc,
            ) from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        truncated = len(rows) > limit
        visible = rows[:limit] if truncated else rows
        return QueryResult(
            sql=sql,
            columns=columns,
            rows=list(visible),
            row_count=len(visible),
            truncated=truncated,
            execution_ms=elapsed_ms,
        )

    # ---------------------------------------------------------- 辅助

    async def _ensure_statement_timeout(self) -> None:
        """按方言设置语句超时（不支持就跳过）。

        设置失败**不**让查询失败 —— 超时只是一层保护，不是查询的前提；
        但必须记 warning，否则"超时没生效"会在生产里悄悄发生。
        """
        ms = int(self._settings.db_query_timeout_s * 1000)
        if self._lease.timeout_ms == ms:
            return  # 同一条连接已经设过，不必每条 SQL 重设
        bound = self.dialect.sql_set_statement_timeout(ms)
        if bound is None:
            logger.debug("%s 不支持语句超时，交由上层整体预算兜底", self.dialect.display_name)
            return

        def _run() -> None:
            with self._lease.cursor() as cursor:
                cursor.execute(bound.sql, bound.params)

        try:
            await self._in_thread(_run)
        except Exception as exc:  # noqa: BLE001 - 超时设置失败不该终止查询
            logger.warning(
                "设置语句超时失败 (%s, %dms): %s: %s",
                self.dialect.display_name,
                ms,
                type(exc).__name__,
                exc,
            )
            return
        self._lease.timeout_ms = ms

    async def _in_thread(self, fn: Callable[[], _T]) -> _T:
        """把阻塞的驱动调用下到线程池。

        驱动是同步阻塞的，直接在协程里调会把事件循环卡住
        （历史上 SSE 流在长查询期间整条断掉就是这个原因）。
        """
        try:
            return await anyio.to_thread.run_sync(fn)
        except DbError:
            raise
        except Exception as exc:
            kind = self.dialect.classify_error(exc)
            raise DbError(
                kind,
                detail=f"{type(exc).__name__}: {exc}",
                user_message=DB_ERROR_MESSAGES[kind],
                cause=exc,
            ) from exc


async def run_query(
    lease: ConnectionLease,
    sql: str,
    *,
    snapshot: SchemaSnapshot | None = None,
    scope: CatalogScope | None = None,
    settings: Settings | None = None,
) -> QueryResult:
    """一次性查询的便捷入口（内部按需建 `SqlExecutor`）。"""
    executor = SqlExecutor(lease, settings)
    return await executor.query(sql, snapshot=snapshot, scope=scope)
