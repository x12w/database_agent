"""后台探索器 —— 大库的"先能用、再逐步补全"。

历史实现（`app/agent/background_explorer.py`）是一个守护线程，两个结构性问题：

1. `_run` 里调 `db.connect_to(profile)`（`:175`）—— 那是把**进程级**连接换成
   后台线程要用的库。前台正在查 A 库时，后台一启动就把连接换成 B 库了；
2. `_tasks` 字典被线程与请求处理协程同时读写，没有任何同步。

这里改成 `asyncio.Task`：自带一条连接租约（`ConnectionManager.acquire`），
状态由一把 `asyncio.Lock` 保护。进度保存在内存里，最终报告写入探索仓储 ——
重启后"是否已探索"由仓储回答（`_is_explored` 语义不变）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache

from dbchat.agent.candidates import (
    priority_order,
)
from dbchat.config import Settings, get_settings
from dbchat.db import get_connection_manager
from dbchat.db.executor import SqlExecutor
from dbchat.db.introspect import load_table_samples
from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.schema import SchemaSnapshot
from dbchat.storage.explorations import ExplorationRepository, get_exploration_repository

__all__ = ["BackgroundExplorer", "get_background_explorer"]

logger = logging.getLogger(__name__)

#: 每处理完这么多张表落一次报告（避免中途失败就全丢）。
_BATCH_SIZE = 5
#: 批次之间的间隔，别把库打满。
_BATCH_PAUSE_S = 0.5


@dataclass(slots=True)
class Progress:
    """后台探索进度。"""

    profile_id: str
    total: int
    sampled: int = 0
    done: bool = False
    priority: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    error: str = ""

    @property
    def completion(self) -> float:
        return self.sampled / max(self.total, 1)

    def to_dict(self) -> dict:
        return {
            "done": self.done,
            "total_tables": self.total,
            "sampled_tables": self.sampled,
            "completion": self.completion,
            "error": self.error,
        }


class BackgroundExplorer:
    """按 profile 管理后台探索任务。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        explorations: ExplorationRepository | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._explorations = explorations or get_exploration_repository()
        self._progress: dict[str, Progress] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    # ---- 生命周期 ----

    async def start(
        self,
        profile: DatabaseProfile,
        snapshot: SchemaSnapshot,
    ) -> bool:
        """启动后台探索。

        Returns:
            False 表示已有任务在跑（或已跑完），本次不重复启动。
        """
        async with self._lock:
            existing = self._tasks.get(profile.id)
            if existing is not None and not existing.done():
                logger.debug("profile %s 的后台探索已在运行", profile.id)
                return False
            state = Progress(
                profile_id=profile.id,
                total=snapshot.table_count,
                priority=priority_order(snapshot.tables, snapshot.foreign_keys),
            )
            self._progress[profile.id] = state
            task = asyncio.create_task(
                self._run(profile, snapshot, state), name=f"dbchat-explore-{profile.id}"
            )
            self._tasks[profile.id] = task
            logger.info("启动后台探索 %s（%d 张表）", profile.id, snapshot.table_count)
            return True

    async def cancel(self, profile_id: str) -> None:
        """取消后台探索（等待任务真正结束）。"""
        async with self._lock:
            task = self._tasks.pop(profile_id, None)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("后台探索 %s 已取消", profile_id)
        except Exception as exc:  # noqa: BLE001 - 取消失败不应影响调用方
            logger.warning("收尾后台探索 %s 时出错: %s: %s", profile_id, type(exc).__name__, exc)

    async def close(self) -> None:
        """应用关闭：取消全部后台任务。"""
        for profile_id in list(self._tasks):
            await self.cancel(profile_id)

    async def progress(self, profile_id: str) -> dict:
        """当前进度；没有在跑的任务时按仓储回答"是否探索过"。"""
        async with self._lock:
            state = self._progress.get(profile_id)
            if state is not None:
                return state.to_dict()
        explored = await self._explorations.ais_explored(profile_id)
        return {
            "done": explored,
            "total_tables": 0,
            "sampled_tables": 0,
            "completion": 1.0 if explored else 0.0,
            "error": "",
        }

    # ---- 主体 ----

    async def _run(
        self,
        profile: DatabaseProfile,
        snapshot: SchemaSnapshot,
        state: Progress,
    ) -> None:
        """后台任务：按优先级给剩余的表补抽样，并逐步落报告。"""
        report_lines: list[str] = []
        sampled: set[str] = set()
        try:
            # 自带一条连接租约 —— 绝不替换别人正在用的连接。
            async with get_connection_manager().acquire(profile) as lease:
                executor = SqlExecutor(lease, self._settings)
                targets = state.priority or list(snapshot.tables)
                for index, table_name in enumerate(targets):
                    meta = snapshot.tables.get(table_name)
                    if meta is None or meta.sample_rows:
                        sampled.add(table_name)
                        continue
                    try:
                        await load_table_samples(
                            executor, {table_name: meta}, limit=self._settings.sample_row_limit
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - 单表失败不中断整轮
                        logger.warning(
                            "后台抽样 %s 失败: %s: %s", table_name, type(exc).__name__, exc
                        )
                    if meta.sample_rows:
                        columns = ", ".join(column.name for column in meta.columns[:8])
                        report_lines.append(f"- **{table_name}**（{meta.row_count} 行）: {columns}")
                    sampled.add(table_name)
                    state.sampled = len(sampled)
                    if (index + 1) % _BATCH_SIZE == 0:
                        await self._persist(state, snapshot, report_lines)
                        await asyncio.sleep(_BATCH_PAUSE_S)
        except asyncio.CancelledError:
            state.error = "cancelled"
            state.done = True
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务失败必须落日志与状态
            logger.warning(
                "后台探索 %s 失败: %s: %s", profile.id, type(exc).__name__, exc, exc_info=True
            )
            state.error = f"{type(exc).__name__}: {exc}"
        finally:
            state.done = True
            try:
                await self._persist(state, snapshot, report_lines)
            except Exception as exc:  # noqa: BLE001 - 落盘失败也要留痕
                logger.warning("后台探索报告落盘失败: %s: %s", type(exc).__name__, exc)

    async def _persist(
        self, state: Progress, snapshot: SchemaSnapshot, report_lines: list[str]
    ) -> None:
        """写入探索报告（报告即"已探索"标记）。"""
        header = [
            f"# 数据库 {snapshot.db_name} 探索报告（后台补全）",
            "",
            f"**表总数**: {snapshot.table_count} | **已抽样**: {state.sampled}",
            f"**外键关系**: {len(snapshot.foreign_keys)}",
            "",
            "## 已补全抽样的表",
        ]
        await self._explorations.asave(
            state.profile_id, "\n".join([*header, *report_lines])
        )


@lru_cache(maxsize=1)
def _shared_explorer() -> BackgroundExplorer:
    return BackgroundExplorer()


def get_background_explorer(settings: Settings | None = None) -> BackgroundExplorer:
    """进程内共享的后台探索器（状态本身有锁保护）。"""
    if settings is not None:
        return BackgroundExplorer(settings)
    return _shared_explorer()
