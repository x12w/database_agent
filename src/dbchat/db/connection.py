"""连接管理 —— 按 profile 持有连接的注册表。

历史实现是一个进程级单例 `db = DatabaseConnection()`（`app/database/
connection.py:125-353`），它有三个结构性毛病：

1. `connect_to()` 会**关掉并替换**那唯一一条连接。两个会话同时查两个库
   时，后一个把前一个的连接掐了。
2. 它还被后台线程（`background_explorer.py:175`）调用，而"当前连接"是
   全局可变状态 —— 前台查到一半，连接被后台换掉了。
3. 由此派生出一个"当前 profile"概念（`db.current_profile`），
   所有模块都得先看它，于是谁都可能拿到别人设的库。

这里的替代是一条注册表：`profile_id -> 连接`，**没有"当前 profile"**。
取用走 `async with manager.acquire(profile) as lease`，同一 profile 的
并发使用由一把 `asyncio.Lock` 串起来（DB-API 连接本来就不能并发用）。

阻塞的驱动调用全部经 `anyio.to_thread.run_sync` 下到线程池，事件循环
不会被 connect/ping/execute 卡住。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio

from dbchat.db.cursor import DialectCursor
from dbchat.db.dialects import get_dialect
from dbchat.db.dialects.base import Connection, Dialect
from dbchat.db.introspect import count_tables_sql
from dbchat.domain.errors import DB_ERROR_MESSAGES, DbError
from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.schema import CatalogScope

__all__ = ["ConnectionLease", "ConnectionManager", "ConnectionProbe", "get_connection_manager"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConnectionLease:
    """一次被借出的连接（含其方言）。"""

    profile: DatabaseProfile
    dialect: Dialect
    connection: Connection
    #: 本条连接建立的时间戳（用于诊断"复用还是新建"）。
    created_at: float
    #: 本条连接被复用的次数。
    reuse_count: int = 0
    #: 已经在这条连接上设置过的语句超时（毫秒）；0 表示尚未设置。
    timeout_ms: int = 0

    def cursor(self) -> DialectCursor:
        """开一个统一游标（字典行 + 方言占位符）。"""
        return DialectCursor(self.dialect.create_cursor(self.connection), self.dialect)

    def raw_cursor(self) -> Any:
        """开一个未经包装的驱动游标（少数字段需要时才用）。"""
        return self.dialect.create_cursor(self.connection)


@dataclass(slots=True)
class ConnectionProbe:
    """连接自检结果。"""

    success: bool
    message: str
    table_count: int = 0


@dataclass(slots=True)
class _Entry:
    """注册表里的一项。"""

    profile: DatabaseProfile
    dialect: Dialect
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connection: Connection | None = None
    created_at: float = 0.0
    reuse_count: int = 0


class ConnectionManager:
    """按 `profile_id` 持有连接的注册表。"""

    def __init__(self, *, connect_timeout_s: int = 30, max_idle_s: float | None = None) -> None:
        """
        Args:
            connect_timeout_s: 建立连接的超时（秒）。
            max_idle_s: 连接最长存活时间（秒）。超过后下次 acquire 会重建，
                避免长期占用服务端会话（达梦等库对空闲会话有上限）。
        """
        self._entries: dict[str, _Entry] = {}
        self._connect_timeout_s = connect_timeout_s
        self._max_idle_s = max_idle_s
        #: 保护 `_entries` 自身的字典改动（不含每项连接的使用）。
        self._registry_lock = asyncio.Lock()

    # ------------------------------------------------------------ 取用

    @asynccontextmanager
    async def acquire(self, profile: DatabaseProfile) -> AsyncIterator[ConnectionLease]:
        """借出一条可用连接；退出时归还（连接保留复用）。

        Raises:
            DbError: 连接建立失败，按 `Dialect.classify_error` 分类。
        """
        entry = await self._entry_for(profile)
        async with entry.lock:
            conn = await self._ensure_live(entry)
            entry.reuse_count += 1
            lease = ConnectionLease(
                profile=profile,
                dialect=entry.dialect,
                connection=conn,
                created_at=entry.created_at,
                reuse_count=entry.reuse_count,
            )
            try:
                yield lease
            finally:
                # 归还时不关连接，只放开使用锁；连接由 close()/close_all() 回收。
                logger.debug("归还连接 %s (复用 %d 次)", profile.id, entry.reuse_count)

    async def _entry_for(self, profile: DatabaseProfile) -> _Entry:
        async with self._registry_lock:
            entry = self._entries.get(profile.id)
            if entry is None:
                entry = _Entry(profile=profile, dialect=get_dialect(profile.kind))
                self._entries[profile.id] = entry
                logger.debug("为 profile %s 注册连接槽位", profile.id)
            elif entry.profile != profile:
                # 同一 id 换了连接参数（用户在设置里改了库）：作废旧连接。
                logger.info("profile %s 的连接参数已变化，重建连接", profile.id)
                await self._dispose(entry)
                entry.profile = profile
                entry.dialect = get_dialect(profile.kind)
            return entry

    async def _ensure_live(self, entry: _Entry) -> Connection:
        """确保 `entry` 上有一条活连接，必要时新建。"""
        conn = entry.connection
        if conn is not None and self._is_expired(entry):
            logger.info("连接 %s 已超过最长存活时间，主动重建", entry.profile.id)
            await self._dispose(entry)
            conn = None

        if conn is not None:
            alive = await anyio.to_thread.run_sync(self._ping, entry.dialect, conn)
            if alive:
                return conn
            logger.warning("profile %s 的连接已失效，重建", entry.profile.id)
            await self._dispose(entry)

        return await self._connect(entry)

    @staticmethod
    def _ping(dialect: Dialect, conn: Connection) -> bool:
        """在线程里探活。探活本身出错按"连接不可用"处理（已在 ping 里记日志）。"""
        try:
            return dialect.ping(conn)
        except Exception as exc:  # noqa: BLE001 - ping 不该抛出，兜底并留痕
            logger.warning("探活时驱动抛出异常: %s: %s", type(exc).__name__, exc)
            return False

    async def _connect(self, entry: _Entry) -> Connection:
        """建立连接（在线程里执行）。"""

        def _do() -> Connection:
            return entry.dialect.connect(entry.profile, timeout_s=self._connect_timeout_s)

        conn = await anyio.to_thread.run_sync(_do)
        entry.connection = conn
        entry.created_at = time.time()
        entry.reuse_count = 0
        logger.info(
            "已连接 %s (%s:%s/%s)",
            entry.dialect.display_name,
            entry.profile.host,
            entry.profile.port,
            entry.profile.database,
        )
        return conn

    def _is_expired(self, entry: _Entry) -> bool:
        if self._max_idle_s is None:
            return False
        return (time.time() - entry.created_at) > self._max_idle_s

    async def _dispose(self, entry: _Entry) -> None:
        """关掉并忘掉一条连接。"""
        conn, entry.connection = entry.connection, None
        if conn is None:
            return
        await anyio.to_thread.run_sync(self._close_quietly, entry.dialect, conn)

    @staticmethod
    def _close_quietly(dialect: Dialect, conn: Connection) -> None:
        try:
            conn.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败只记日志，不改变调用方结果
            logger.warning("关闭连接失败: %s: %s", type(exc).__name__, exc)

    # ------------------------------------------------------------ 回收

    async def close(self, profile_id: str) -> None:
        """关闭某个 profile 的连接（不存在时什么也不做）。"""
        async with self._registry_lock:
            entry = self._entries.pop(profile_id, None)
        if entry is None:
            return
        async with entry.lock:
            await self._dispose(entry)

    async def close_all(self) -> None:
        """关闭全部连接（进程退出时调用）。"""
        async with self._registry_lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            async with entry.lock:
                await self._dispose(entry)

    def connected_profile_ids(self) -> frozenset[str]:
        """当前持有连接的 profile id（诊断用）。"""
        return frozenset(pid for pid, e in self._entries.items() if e.connection is not None)

    # ------------------------------------------------------------ 自检

    async def probe(self, profile: DatabaseProfile) -> ConnectionProbe:
        """连接自检：连一次、数一下表、立刻断开。

        用 `sql_count_tables`（方言提供）而不是把库名拼进 SQL —— 既走
        真实的内省路径，也不往 SQL 里插配置值。
        """
        dialect = get_dialect(profile.kind)

        def _do() -> ConnectionProbe:
            conn = dialect.connect(profile, timeout_s=self._connect_timeout_s)
            try:
                bound = count_tables_sql(dialect, _EMPTY_SCOPE)
                with DialectCursor(dialect.create_cursor(conn), dialect) as cur:
                    cur.execute(bound.sql, bound.params)
                    row = cur.fetchone()
                count = int(row.get("table_count", 0)) if row else 0
                return ConnectionProbe(
                    success=True,
                    message=f"连接成功（{dialect.display_name}），{profile.database} 包含 {count} 张表",
                    table_count=count,
                )
            finally:
                self._close_quietly(dialect, conn)

        try:
            return await anyio.to_thread.run_sync(_do)
        except DbError as exc:
            return ConnectionProbe(success=False, message=exc.user_message)
        except Exception as exc:  # noqa: BLE001 - 自检失败要变成结果而不是异常
            logger.warning("连接自检失败 %s: %s: %s", profile.id, type(exc).__name__, exc)
            kind = dialect.classify_error(exc)
            return ConnectionProbe(success=False, message=DB_ERROR_MESSAGES[kind])


#: 自检 SQL 不需要表筛选范围。
_EMPTY_SCOPE = CatalogScope()

_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    """进程内默认连接管理器。

    注意这是"注册表"而不是"当前连接"：它不记录谁在用哪个库，
    调用方每次都显式传入 `profile`。
    """
    global _manager
    if _manager is None:
        from dbchat.config import get_settings

        settings = get_settings()
        _manager = ConnectionManager(
            connect_timeout_s=settings.db_connect_timeout_s,
        )
    return _manager
