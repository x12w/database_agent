"""首次连接一个数据库时的引导：拿结构 + 首轮探索。

对应历史实现的 `orchestrator.explore_database`（`:1218-1415`），但换掉了
三种做法：

1. **结构只采一次**。历史实现先用 `SELECT COUNT(*)` 数表（按库类型三向分支），
   再让探索 agent 自己用 `list_tables`/`describe_table` 现场查
   `information_schema` —— 同一份结构被读了三次，还可能互相不一致。
   这里统一走 `db.introspect.build_snapshot`，快照同时供探索 agent 的工具使用。
2. **大库判定用配置**（`Settings.large_db_table_threshold`），不再让
   prompt_builder、orchestrator、background_explorer 各持一个 `20`。
3. **超时用 `asyncio.wait_for`**（`Settings.schema_load_timeout_s`），
   不再起"线程池 + future.result(timeout=60)"的临时线程。

大库走"快速结构 + 优先级抽样 + 后台补全"；小库走一轮 LLM 探索，产出报告并落库
（报告即"已探索"标记，`is_explored` 语义与历史一致）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio

from dbchat.agent import AgentRun, explore_spec, run_agent_loop
from dbchat.agent.background import BackgroundExplorer, get_background_explorer
from dbchat.agent.candidates import FAST_EXPLORE_TABLE_COUNT, priority_order
from dbchat.config import Settings
from dbchat.db import build_snapshot, get_connection_manager
from dbchat.db.executor import SqlExecutor
from dbchat.db.introspect import count_tables_sql
from dbchat.domain.errors import DbError, DbErrorKind
from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.schema import CatalogScope, SchemaSnapshot, scope_cache_key
from dbchat.events.base import STAGE_LABELS, Stage
from dbchat.events.chat import ChatEvent, StageFinished, StageStarted
from dbchat.events.project import RunView, project
from dbchat.llm.client import LLMClient
from dbchat.storage.explorations import ExplorationRepository, get_exploration_repository
from dbchat.tools import ToolRegistry, get_registry
from dbchat.usecases.contracts import RunContext

logger = logging.getLogger(__name__)

__all__ = [
    "SchemaStore",
    "build_schema",
    "ensure_schema",
    "explore_database",
    "exploration_report",
    "get_schema_store",
]

#: 首次加载结构时，如果快照里已有表数就不再重复数表。
_PROGRESS_TABLE = "tables"


async def ensure_schema(
    ctx: RunContext,
    *,
    settings: Settings,
    llm: LLMClient,
    registry: ToolRegistry | None = None,
    explorations: ExplorationRepository | None = None,
    backgrounds: BackgroundExplorer | None = None,
    clarifications: Any | None = None,
    schema_store: Any | None = None,
) -> AsyncIterator[ChatEvent]:
    """确保 `ctx.snapshot` 可用（必要时先做首轮探索）。

    产出线上事件（探索期间的活动与阶段），并把快照写回 `ctx.snapshot`。
    调用方在耗尽这个生成器之后读 `ctx.snapshot`。
    """
    active_registry = registry or get_registry()
    repository = explorations or get_exploration_repository()
    explorer = backgrounds or get_background_explorer(settings)

    if ctx.snapshot is None and schema_store is not None:
        ctx.snapshot = await _cached_snapshot(schema_store, ctx)
    if ctx.snapshot is not None:
        return

    try:
        explored = await repository.ais_explored(ctx.profile.id)
    except Exception as exc:  # noqa: BLE001 - 读不到就当没探索过（更安全）
        logger.warning("读取探索状态失败: %s: %s", type(exc).__name__, exc)
        explored = False

    if not explored:
        async for event in explore_database(
            ctx,
            settings=settings,
            llm=llm,
            registry=active_registry,
            explorations=repository,
            backgrounds=explorer,
            clarifications=clarifications,
            schema_store=schema_store,
        ):
            yield event

    if ctx.snapshot is None:
        ctx.snapshot = await build_schema(
            ctx, settings=settings, schema_store=schema_store
        )


async def _cached_snapshot(store: Any, ctx: RunContext) -> SchemaSnapshot | None:
    """从结构缓存取快照（缓存坏了不能影响提问，但必须记日志）。"""
    getter = getattr(store, "get", None)
    if getter is None:
        return None
    try:
        snapshot = getter(ctx.profile, ctx.scope)
        if hasattr(snapshot, "__await__"):
            snapshot = await snapshot
    except Exception as exc:  # noqa: BLE001 - 缓存只是加速手段
        logger.warning("读取结构缓存失败: %s: %s", type(exc).__name__, exc)
        return None
    return snapshot if isinstance(snapshot, SchemaSnapshot) else None


async def _store_snapshot(store: Any | None, ctx: RunContext, snapshot: SchemaSnapshot) -> None:
    """把新采的快照写回缓存（可选协作对象，接口不存在就跳过）。"""
    if store is None:
        return
    for name in ("put", "save", "set"):
        setter = getattr(store, name, None)
        if setter is None:
            continue
        try:
            result = setter(ctx.profile, ctx.scope, snapshot)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001 - 写缓存失败只记日志
            logger.warning("写入结构缓存失败: %s: %s", type(exc).__name__, exc)
        return


async def build_schema(
    ctx: RunContext, *, settings: Settings, schema_store: Any | None = None
) -> SchemaSnapshot:
    """采集（或从缓存取）结构快照。

    Raises:
        DbError: 连接失败或超时 —— 由管道转成线上 `run.failed`。
    """
    if ctx.snapshot is not None:
        return ctx.snapshot

    manager = get_connection_manager()
    async with manager.acquire(ctx.profile) as lease:
        executor = SqlExecutor(lease, settings)
        table_count = await _count_tables(executor, ctx)
        fast = table_count > settings.large_db_table_threshold
        logger.info(
            "加载结构 %s（%s 张表，%s 模式）",
            ctx.profile.display_name,
            table_count,
            "快速" if fast else "完整",
        )
        try:
            snapshot = await asyncio.wait_for(
                build_snapshot(
                    executor,
                    ctx.scope,
                    fast=fast,
                    sample_rows=settings.sample_row_limit,
                ),
                timeout=float(settings.schema_load_timeout_s),
            )
        except TimeoutError as exc:
            raise DbError(
                DbErrorKind.TIMEOUT,
                user_message=(
                    f"数据库结构加载超时（超过 {settings.schema_load_timeout_s} 秒），"
                    "可能是数据库过大或连接缓慢"
                ),
                detail=f"schema load timeout after {settings.schema_load_timeout_s}s",
                cause=exc,
            ) from exc
    await _store_snapshot(schema_store, ctx, snapshot)
    return snapshot


async def _count_tables(executor: SqlExecutor, ctx: RunContext) -> int:
    """先数一下表数量，用来决定走快速还是完整模式。"""
    bound = count_tables_sql(executor.dialect, ctx.scope)
    rows = await executor.fetch_all(bound)
    if not rows:
        return 0
    first = rows[0]
    for key in ("table_count", "cnt", "COUNT(*)"):
        if key in first:
            try:
                return int(first[key] or 0)
            except (TypeError, ValueError):
                continue
    # 方言没给 COUNT，退回"行数即表数"（清单查询本来就一行一张表）。
    return len(rows)


async def explore_database(
    ctx: RunContext,
    *,
    settings: Settings,
    llm: LLMClient,
    registry: ToolRegistry,
    explorations: ExplorationRepository,
    backgrounds: BackgroundExplorer,
    clarifications: Any | None = None,
    schema_store: Any | None = None,
) -> AsyncIterator[ChatEvent]:
    """首轮探索：小库跑一轮 LLM 探索，大库走快速结构 + 后台补全。"""
    view = RunView(run_id=ctx.run_id, now=time.time())
    started = time.monotonic()
    yield StageStarted(
        seq=view.next_seq(),
        ts=time.time(),
        run_id=ctx.run_id,
        stage=Stage.UNDERSTANDING,
        label=STAGE_LABELS[Stage.UNDERSTANDING],
        index=1,
        total=5,
        detail=f"首次连接 {ctx.profile.display_name}，开始探索数据库",
    )

    # 第一步：结构（探索 agent 的工具也读这份快照）
    ctx.snapshot = await build_schema(ctx, settings=settings, schema_store=schema_store)
    table_count = ctx.snapshot.table_count
    if table_count == 0:
        yield StageFinished(
            seq=view.next_seq(),
            ts=time.time(),
            run_id=ctx.run_id,
            stage=Stage.UNDERSTANDING,
            ok=False,
            summary="无法读取数据库表结构，请检查连接和权限",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return

    if table_count > settings.large_db_table_threshold:
        # ---- 大库：先落一份快速报告，细节交给后台任务 ----
        await _save_large_report(ctx, explorations)
        await backgrounds.start(ctx.profile, ctx.snapshot)
        logger.info("大库 %s：已启动后台探索（%d 张表）", ctx.profile.id, table_count)
        yield StageFinished(
            seq=view.next_seq(),
            ts=time.time(),
            run_id=ctx.run_id,
            stage=Stage.UNDERSTANDING,
            ok=True,
            summary=f"{table_count} 张表已索引，后台继续补全抽样",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        return

    # ---- 小库：跑一轮探索 agent ----
    spec = await explore_spec(
        settings,
        registry=registry,
        dialect=get_dialect_for(ctx),
        db_name=ctx.snapshot.db_name,
        save_report=lambda report: _save_report(ctx, explorations, report),
    )
    run = AgentRun(
        ctx=ctx,
        llm=llm,
        registry=registry,
        settings=settings,
        clarifications=clarifications,
    )
    messages = [_explore_user_message(ctx)]
    async for event in run_agent_loop(spec, run, messages):
        mapped = project(event, _touch(view))
        if mapped is not None:
            yield mapped
    # 报告由 spec 的 finish_extract 落库（`_save_report`），这里只关心事件。

    yield StageFinished(
        seq=view.next_seq(),
        ts=time.time(),
        run_id=ctx.run_id,
        stage=Stage.UNDERSTANDING,
        ok=True,
        summary=f"探索完成（{table_count} 张表）",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def exploration_report(ctx: RunContext, repository: ExplorationRepository | None = None) -> str:
    """读探索报告（没有则空串）。"""
    repo = repository or get_exploration_repository()
    try:
        return await repo.areport(ctx.profile.id)
    except Exception as exc:  # noqa: BLE001 - 报告缺失不影响主流程
        logger.info("读取探索报告失败: %s: %s", type(exc).__name__, exc)
        return ""


async def _save_report(
    ctx: RunContext, repository: ExplorationRepository, report: str
) -> None:
    """落探索报告 —— 报告存在即"已探索"，避免对同一个库反复全量探索。"""
    try:
        await repository.asave(ctx.profile.id, report)
    except Exception as exc:  # noqa: BLE001 - 落盘失败必须留痕，否则会重复探索
        logger.warning("保存探索报告失败: %s: %s", type(exc).__name__, exc)


async def _save_large_report(ctx: RunContext, repository: ExplorationRepository) -> None:
    """大库的快速报告：表索引 + 高优先级表 + 外键。"""
    snapshot = ctx.snapshot
    if snapshot is None:
        return
    order = priority_order(snapshot.tables, snapshot.foreign_keys)
    sampled = [name for name in order[:FAST_EXPLORE_TABLE_COUNT] if snapshot.tables[name].sample_rows]

    lines = [
        f"# 数据库 {snapshot.db_name} 探索报告（快速模式）",
        "",
        f"**表总数**: {snapshot.table_count} | **外键关系**: {len(snapshot.foreign_keys)}",
        f"**探索模式**: 快速（已抽样 {len(sampled)} 张高优先级表，其余在后台继续）",
        "",
        "## 高优先级表概览",
    ]
    for name in sampled or order[:FAST_EXPLORE_TABLE_COUNT]:
        meta = snapshot.tables[name]
        columns = ", ".join(column.name for column in meta.columns[:6])
        lines.append(f"- **{name}**（{meta.row_count} 行）: {columns}")

    if snapshot.foreign_keys:
        lines.append("")
        lines.append("## 主要外键关系")
        for fk in snapshot.foreign_keys[:20]:
            lines.append(f"- `{fk.table}`.{fk.column} → `{fk.ref_table}`.{fk.ref_column}")
    await _save_report(ctx, repository, "\n".join(lines))


def get_dialect_for(ctx: RunContext):
    """当前连接对应的方言实例。"""
    from dbchat.db.dialects import get_dialect

    return get_dialect(ctx.profile.kind)


def _explore_user_message(ctx: RunContext):
    from dbchat.llm.messages import user_message

    name = ctx.snapshot.db_name if ctx.snapshot else ctx.profile.display_name
    return user_message(f"请探索数据库 {name} 并生成概览报告。")


def _touch(view: RunView) -> RunView:
    view.now = time.time()
    return view


# ------------------------------------------------------------ 结构快照缓存


class SchemaStore:
    """结构快照的磁盘缓存（`var/schema/`）。

    API 层把它作为可选协作对象注入（`di._OPTIONAL_COLLABORATORS` 约定的
    `dbchat.usecases.onboarding.get_schema_store(settings)`）：

    * 每次请求先用 `get()` 拿一份缓存快照，命中就不必重新内省（大库首次
      内省要几十秒）；
    * `put()` 在采集完成后写入；
    * `invalidate()` 供"刷新/重建缓存"路由调用（`di.invalidate_snapshot`
      会调它，同时还有一份"按文件名删除"的兜底）。

    文件按 `domain.schema.scope_cache_key()` 命名 —— 表筛选范围必须进键，
    否则一次筛选后的快照会覆盖全量快照（历史实现真实踩过这个坑）。

    TTL 过期即视为未命中：重新内省一次，语义比"拿旧结构硬撑"更安全。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get(self, profile: DatabaseProfile, scope: CatalogScope) -> SchemaSnapshot | None:
        """读缓存；未命中/过期/损坏都返回 None（并记日志）。"""
        path = self._path(profile.id, scope)
        return await anyio.to_thread.run_sync(self._read, path)

    async def put(
        self, profile: DatabaseProfile, scope: CatalogScope, snapshot: SchemaSnapshot
    ) -> None:
        """写缓存（原子替换，避免读到写了一半的文件）。"""
        path = self._path(profile.id, scope)
        payload = json.dumps(snapshot.to_json_dict(), ensure_ascii=False, default=str)
        await anyio.to_thread.run_sync(self._write, path, payload)

    async def invalidate(self, profile_id: str) -> int:
        """删掉某 profile 的全部缓存文件，返回删除数量。"""
        return await anyio.to_thread.run_sync(self._delete, profile_id)

    # ---- 同步实现（在线程池里跑）----

    def _path(self, profile_id: str, scope: CatalogScope) -> Path:
        return self._settings.schema_cache_dir / f"{scope_cache_key(profile_id, scope)}.json"

    def _read(self, path: Path) -> SchemaSnapshot | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读取结构缓存失败 %s: %s: %s", path.name, type(exc).__name__, exc)
            return None

        age = time.time() - float(raw.get("captured_at") or 0.0)
        if age > float(self._settings.schema_cache_ttl_s):
            logger.info("结构缓存已过期（%.0f 秒 > TTL），重新采集", age)
            return None
        try:
            snapshot = SchemaSnapshot.from_json_dict(raw)
        except (KeyError, ValueError, TypeError) as exc:
            # 格式版本不匹配等：当作没有缓存，别让旧格式把请求打挂。
            logger.warning("结构缓存不可用 %s: %s: %s", path.name, type(exc).__name__, exc)
            return None
        return snapshot

    def _write(self, path: Path, payload: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.warning("写入结构缓存失败 %s: %s: %s", path.name, type(exc).__name__, exc)

    def _delete(self, profile_id: str) -> int:
        removed = 0
        try:
            for path in self._settings.schema_cache_dir.glob(f"{profile_id}*.json"):
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("删除结构缓存 %s 失败: %s", path.name, exc)
        except OSError as exc:
            logger.warning("枚举结构缓存目录失败: %s: %s", type(exc).__name__, exc)
        return removed


def get_schema_store(settings: Settings) -> SchemaStore:
    """结构缓存工厂（`di._OPTIONAL_COLLABORATORS` 按名字找的就是它）。

    对象本身无状态（只持一份路径配置），构造很便宜，不做缓存 ——
    `Settings` 不是可哈希对象，lru_cache 在这里用不了。
    """
    return SchemaStore(settings)
