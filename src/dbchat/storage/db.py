"""SQLite 连接、迁移与执行基座。

并发模型：**单连接 + 单写锁**。应用是单进程的，SQLite 的 WAL 允许读写并发，
但写只有一个，所以用一个 `threading.RLock` 串行化所有访问即可 —— 比连接池
简单，也不会出现"读到别的线程写了一半的状态"。连接开启
`check_same_thread=False`，因为异步路径会把访问丢进线程池执行。

异步路径：所有仓储同时提供同步方法与 `a` 前缀的异步孪生（由
`OffloadableRepo` 自动生成），异步方在 `anyio.to_thread` 中执行，
事件循环不会因为 SQLite 的磁盘 IO 停摆。
"""

from __future__ import annotations

import inspect
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial, wraps
from pathlib import Path
from typing import Any, TypeVar

import anyio

from dbchat.config import Settings, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 迁移脚本目录（随包发布，不是运行时路径）。
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

#: 迁移记录表名。
MIGRATIONS_TABLE = "schema_migrations"

#: 等锁超时（毫秒）。Settings 补上同名字段后可覆盖。
DEFAULT_BUSY_TIMEOUT_MS = 5000

#: 时间戳格式 —— 与历史 JSON 完全一致（UTC，秒精度，可直接字典序比较）。
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_now_str() -> str:
    """当前 UTC 时间（`YYYY-MM-DD HH:MM:SS`）。"""
    return datetime.now(UTC).strftime(TIME_FORMAT)


class Storage:
    """一个 SQLite 库的连接与访问入口。

    Args:
        settings: 配置来源；库文件取 `Settings.db_path`。
        path: 直接指定库文件（测试用），优先于 `settings.db_path`。
    """

    def __init__(self, settings: Settings | None = None, *, path: str | Path | None = None) -> None:
        self._settings = settings or get_settings()
        self._path = Path(path) if path is not None else self._settings.db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._tx_depth = 0

    # ---- 基本信息 ----

    @property
    def path(self) -> Path:
        """库文件路径。"""
        return self._path

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"Storage(path={self._path!r})"

    # ---- 连接与迁移 ----

    @property
    def busy_timeout_ms(self) -> int:
        override = getattr(self._settings, "sqlite_busy_timeout_ms", None)
        return int(override) if override else DEFAULT_BUSY_TIMEOUT_MS

    def connection(self) -> sqlite3.Connection:
        """取得连接（首次调用时建库 + 迁移）。"""
        with self._lock:
            if self._conn is None:
                conn = self._open()
                self._apply_migrations(conn)
                self._conn = conn
            return self._conn

    def _open(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None：交给本类显式管理事务（BEGIN IMMEDIATE / COMMIT），
        # 避免 sqlite3 隐式开事务导致的"锁升级"竞争。
        conn = sqlite3.connect(
            self._path,
            timeout=self.busy_timeout_ms / 1000,
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _apply_migrations(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row["version"] for row in conn.execute(f"SELECT version FROM {MIGRATIONS_TABLE}")
        }
        for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = script.stem
            if version in applied:
                continue
            logger.info("应用迁移 %s", version)
            conn.executescript(script.read_text(encoding="utf-8"))
            conn.execute(
                f"INSERT OR REPLACE INTO {MIGRATIONS_TABLE} (version, applied_at) VALUES (?, ?)",
                (version, utc_now_str()),
            )

    def migrate(self) -> None:
        """显式执行迁移（幂等）。"""
        self.connection()

    def close(self) -> None:
        """关闭连接。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- 同步执行 ----

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """查询多行。"""
        with self._lock:
            return list(self.connection().execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        """查询一行。"""
        with self._lock:
            return self.connection().execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行一条写语句，返回 lastrowid。"""
        with self._lock:
            cursor = self.connection().execute(sql, tuple(params))
            return int(cursor.lastrowid or 0)

    @contextmanager
    def _tx_scope(self) -> Iterator[sqlite3.Connection]:
        """写事务作用域。

        多语句原子性靠显式 `BEGIN IMMEDIATE` —— 它立刻拿写锁，避免
        读→写升级过程中被别的连接插队（`database is locked` 的经典来源）。
        嵌套调用复用外层事务（`_tx_depth`），不会中途提交。
        """
        with self._lock:
            conn = self.connection()
            if self._tx_depth == 0:
                conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield conn
            except BaseException:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    conn.rollback()
                raise
            self._tx_depth -= 1
            if self._tx_depth == 0:
                conn.commit()

    def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """在写事务中执行 `fn`。"""
        with self._tx_scope() as conn:
            return fn(conn)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """写事务上下文（供仓储做多语句原子操作）。"""
        with self._tx_scope() as conn:
            yield conn

    # ---- 异步孪生 ----

    async def arun(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """`run` 的异步版本。"""
        return await anyio.to_thread.run_sync(self.run, fn)

    async def aquery(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """`query` 的异步版本。"""
        return await anyio.to_thread.run_sync(partial(self.query, sql, params))

    async def aquery_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        """`query_one` 的异步版本。"""
        return await anyio.to_thread.run_sync(partial(self.query_one, sql, params))

    async def aexecute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """`execute` 的异步版本。"""
        return await anyio.to_thread.run_sync(partial(self.execute, sql, params))


def _offload[T](fn: Callable[..., T]) -> Callable[..., Any]:
    """把一个同步方法包装成在线程池里执行的异步方法。"""

    @wraps(fn)
    async def twin(self: Any, *args: Any, **kwargs: Any) -> T:
        return await anyio.to_thread.run_sync(partial(fn, self, *args, **kwargs))

    twin.__doc__ = f"{fn.__doc__ or ''}\n\n（异步孪生：在线程池中执行，不阻塞事件循环。）".strip()
    return twin


class OffloadableRepo:
    """仓储基类：为每个同步方法自动生成 `a` 前缀的异步孪生。

    约定：
    * 同步方法写业务逻辑，名字**不要**以 `a` 开头（否则孪生名会撞）；
    * `axxx()` 在线程池里调用 `xxx()`，事件循环不被 SQLite 的磁盘 IO 阻塞；
    * 显式定义的 `axxx` 优先，不会被覆盖；
    * `@staticmethod` / `@classmethod` / `@property` 不生成孪生。

    这样"每个仓储方法都有异步版本"是结构保证的，而不是靠人记得写 ——
    漏写一个就意味着一条阻塞事件循环的调用路径。
    """

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, attr in list(vars(cls).items()):
            if name.startswith("_") or not inspect.isfunction(attr):
                continue
            twin = f"a{name}"
            if twin in vars(cls):
                logger.debug("%s.%s 已显式定义，跳过自动生成", cls.__name__, twin)
                continue
            setattr(cls, twin, _offload(attr))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    """`sqlite3.Row` → dict（None 安全）。"""
    return dict(row) if row is not None else {}


def rows_to_dicts(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    """行列表 → dict 列表。"""
    return [dict(row) for row in rows]


def load_json[T](raw: str | None, default: T) -> T:
    """把库里的 JSON 文本读回来；损坏时记日志并返回默认值。"""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("持久化的 JSON 损坏，按默认值处理: %s", exc)
        return default


def dump_json(value: Any) -> str | None:
    """序列化为 JSON 文本；None/空值存 NULL。"""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


_storage_cache: dict[str, Storage] = {}
_storage_lock = threading.Lock()


def get_storage(settings: Settings | None = None) -> Storage:
    """进程内共享的 Storage（按库文件路径缓存）。"""
    s = settings or get_settings()
    key = str(s.db_path)
    with _storage_lock:
        storage = _storage_cache.get(key)
        if storage is None:
            storage = Storage(s)
            _storage_cache[key] = storage
        return storage


def reset_storage_cache() -> None:
    """清空进程内缓存（测试用）。"""
    with _storage_lock:
        for storage in _storage_cache.values():
            storage.close()
        _storage_cache.clear()


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "MIGRATIONS_DIR",
    "MIGRATIONS_TABLE",
    "TIME_FORMAT",
    "OffloadableRepo",
    "Storage",
    "dump_json",
    "get_storage",
    "load_json",
    "reset_storage_cache",
    "row_to_dict",
    "rows_to_dicts",
    "utc_now_str",
]
