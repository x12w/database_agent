"""依赖注入 —— 应用级上下文（AppContext）与按请求的运行上下文（RunContext）。

历史实现的全局可变状态有三处：`app.database.connection.db` 单例（进程内唯一的
"当前连接"）、`app/main.py` 的 `sessions` dict（内存会话）、两个模块级
`_active_table_filter`。它们都不属于任何一次请求，于是两个并发请求会互相串改 ——
后一个 `connect_to()` 会把前一个正在用的连接关掉。

这里按生命周期切开：

* **AppContext** —— 属于应用。lifespan 里构建一次，挂在 `app.state` 上，
  靠 FastAPI 依赖取用。模块级不再有任何可变状态。
* **RunContext** —— 属于某一次提问。每个请求新建，请求结束即弃。

连接不再是"当前连接"而是**按 profile 的注册表**：`AppContext.lease()` 借出，
`ConnectionManager` 保证同一 profile 的并发使用被串行化，不同 profile 互不
影响 —— 这正是历史实现做不到的事。
"""

from __future__ import annotations

import glob
import importlib
import inspect
import logging
import sqlite3
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import Depends, HTTPException, Query, Request

from dbchat.config import Settings, get_settings
from dbchat.db import get_connection_manager
from dbchat.db.connection import ConnectionLease, ConnectionManager
from dbchat.domain.errors import DbChatError
from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.schema import CatalogScope
from dbchat.llm.client import LLMClient
from dbchat.rag.indexer import RAGIndexer
from dbchat.storage import (
    ClarificationRepository,
    Conversation,
    ConversationRepository,
    ExplorationRepository,
    ProfileRepository,
    Storage,
)
from dbchat.storage.db import dump_json, utc_now_str
from dbchat.usecases.contracts import ChatUseCase, ExploreUseCase, RunContext

logger = logging.getLogger(__name__)

__all__ = [
    "AppContext",
    "ChatUseCaseDep",
    "CtxDep",
    "ExploreUseCaseDep",
    "RunContextDep",
    "build_context",
    "ensure_conversation",
    "get_chat_use_case",
    "get_ctx",
    "get_explore_use_case",
    "get_run_context",
    "invalidate_snapshot",
]

#: `build_context` 的"未指定"哨兵：与显式传入的 `None`（= 明确不使用）区分开。
_AUTO = object()


@dataclass
class AppContext:
    """一次进程生命周期的全部协作对象。

    刻意不提供模块级单例：取用一律走 `get_ctx()`（读 `app.state`）。
    """

    settings: Settings
    storage: Storage
    connections: ConnectionManager
    profiles: ProfileRepository
    conversations: ConversationRepository
    clarifications: ClarificationRepository
    explorations: ExplorationRepository
    llm: LLMClient
    rag: RAGIndexer
    #: 结构快照缓存的可选实现（agent 运行时注入）。为 None 时，
    #: `invalidate_snapshot()` 直接清 `Settings.schema_cache_dir` 下的缓存文件。
    schema_store: Any | None = None
    #: 工具注册表与计算沙箱 —— API 层不直接使用，只替 agent 运行时持有。
    tools: Any | None = None
    sandbox: Any | None = None
    #: 后台任务（RAG 索引构建等），退出时统一取消。
    background_tasks: set[Any] = field(default_factory=set)
    #: 在用租约计数（profile_id → 条数）。只用于诊断与测试断言并发是否收干净。
    _leases_in_use: dict[str, int] = field(default_factory=dict)
    #: 惰性构造的用例实现（`get_chat_use_case` 缓存）。
    _chat_use_case: ChatUseCase | None = None
    _explore_use_case: ExploreUseCase | None = None

    # ---------------------------------------------------------------- 连接

    @property
    def in_use_leases(self) -> int:
        """当前在用租约总数。请求结束后必须回到 0 —— 否则说明连接泄漏。"""
        return sum(self._leases_in_use.values())

    @asynccontextmanager
    async def lease(self, profile: DatabaseProfile) -> AsyncIterator[ConnectionLease]:
        """借出一条连接租约，并记账（退出时归还）。

        Raises:
            DbError: 连接建立失败（文案已是中文，可直接给用户看）。
        """
        self._leases_in_use[profile.id] = self._leases_in_use.get(profile.id, 0) + 1
        try:
            async with self.connections.acquire(profile) as lease:
                yield lease
        finally:
            remaining = self._leases_in_use.get(profile.id, 1) - 1
            if remaining > 0:
                self._leases_in_use[profile.id] = remaining
            else:
                self._leases_in_use.pop(profile.id, None)

    # ------------------------------------------------------------ 连接配置

    async def list_profiles(self) -> list[DatabaseProfile]:
        """全部连接配置（空表时用 .env 的默认配置初始化一份）。"""
        return await self.profiles.aensure_default(self.settings.default_profile())

    async def resolve_profile(self, profile_id: str | None) -> DatabaseProfile:
        """把请求里的 profile_id 解析成配置；未知 id 一律 404。

        `profile_id` 为空表示"用默认库"—— 即配置列表里的第一个（历史上
        `.env` 的默认连接排在最前）。这里**不会**退回到"当前连接"，因为
        那个概念已经不存在了。

        走 `list_profiles()` 而不是直接 `get()`：全新安装时配置表是空的，
        首次请求要顺带把 `.env` 的默认连接写进去，否则连默认库都"不存在"。
        """
        profiles = await self.list_profiles()
        if profile_id:
            for profile in profiles:
                if profile.id == profile_id:
                    return profile
            raise HTTPException(status_code=404, detail=f"数据库配置不存在: {profile_id}")

        if not profiles:
            raise HTTPException(status_code=404, detail="尚未配置任何数据库连接")
        return profiles[0]

    # ------------------------------------------------------------ 用例实现

    def chat_use_case(self) -> ChatUseCase:
        """聊天用例实现（惰性导入，未落地时给出明确报错）。"""
        if self._chat_use_case is None:
            self._chat_use_case = _build_chat_use_case(self)
        return self._chat_use_case

    def explore_use_case(self) -> ExploreUseCase:
        """主动探索用例实现（惰性导入）。"""
        if self._explore_use_case is None:
            self._explore_use_case = _build_explore_use_case(self)
        return self._explore_use_case

    # ---------------------------------------------------------------- 回收

    async def aclose(self) -> None:
        """关闭连接与本地存储（进程退出时调用）。"""
        for task in list(self.background_tasks):
            task.cancel()
        self.background_tasks.clear()
        await self.connections.close_all()
        self.storage.close()


def build_context(
    settings: Settings | None = None,
    *,
    storage: Storage | None = None,
    connections: ConnectionManager | None = None,
    llm: LLMClient | None = None,
    rag: RAGIndexer | None = None,
    schema_store: Any = _AUTO,
    tools: Any = _AUTO,
    sandbox: Any = _AUTO,
) -> AppContext:
    """构建应用上下文。

    Args:
        settings: 配置；不传取进程内单例。
        storage / connections / llm / rag: 显式覆盖（测试与将来的多租户用）。
            `connections` 缺省用 `dbchat.db.get_connection_manager()` —— 工具层
            的 `ToolContext.database()` 走的也是它，自建一个管理器会让同一个库
            出现两条连接。
        schema_store / tools / sandbox: 可选协作对象。默认按约定名惰性导入
            （见 `_OPTIONAL_COLLABORATORS`），未落地时按 None 处理。
    """
    s = settings or get_settings()
    store = storage or Storage(s)
    return AppContext(
        settings=s,
        storage=store,
        connections=connections or get_connection_manager(),
        profiles=ProfileRepository(store),
        conversations=ConversationRepository(store),
        clarifications=ClarificationRepository(store),
        explorations=ExplorationRepository(store),
        llm=llm or LLMClient(s),
        rag=rag or RAGIndexer(s),
        schema_store=_load_optional("schema_store", s) if schema_store is _AUTO else schema_store,
        tools=_load_optional("tools", s) if tools is _AUTO else tools,
        sandbox=_load_optional("sandbox", s) if sandbox is _AUTO else sandbox,
    )


#: 可选协作对象的约定位置：`(模块, 属性, 是否工厂)`。
#: 工厂 = 形如 `f(settings)` 的构造函数；非工厂 = 直接取那个对象（如函数/类）。
#: agent 运行时（并行开发）落地后无需改本文件即可接上。
_OPTIONAL_COLLABORATORS: dict[str, tuple[str, str, bool]] = {
    "tools": ("dbchat.tools", "get_registry", True),
    "sandbox": ("dbchat.tools.sandbox", "run_python_code", False),
    # 结构快照缓存目前由 usecases.onboarding.ensure_schema 负责（走 var/schema
    # 磁盘缓存），没有独立对象可解析；留空即走 `invalidate_snapshot` 的文件清理。
    "schema_store": ("dbchat.usecases.onboarding", "get_schema_store", True),
}


def _load_optional(what: str, settings: Settings) -> Any | None:
    """按约定名导入可选协作对象；缺了只记日志，不阻断应用启动。"""
    module_name, attr_name, is_factory = _OPTIONAL_COLLABORATORS[what]
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        logger.info("%s 尚未提供（%s 不可导入），按「无」处理: %s", what, module_name, exc)
        return None
    found = getattr(module, attr_name, None)
    if found is None:
        logger.debug("%s 的模块 %s 中没有 %s，按「无」处理", what, module_name, attr_name)
        return None
    return found(settings) if is_factory else found


def _build_chat_use_case(ctx: AppContext) -> ChatUseCase:
    """构造聊天用例。

    约定的工厂名是 `build_chat_use_case(ctx)`（`dbchat.usecases.chat`），
    也接受 `build_pipeline(**)` 这类显式传参的构造器 —— 后者按签名过滤关键字，
    免得用例的构造参数一变，路由就在运行时 503。
    """
    factory = _load_factory(
        "dbchat.usecases.chat", ("build_chat_use_case", "build_pipeline"), what="聊天用例"
    )
    return _call_factory(
        factory,
        ctx,
        {
            "settings": ctx.settings,
            "llm": ctx.llm,
            "registry": ctx.tools,
            "conversations": ctx.conversations,
            "clarifications": ctx.clarifications,
            "explorations": ctx.explorations,
            "rag": ctx.rag,
            "schema_store": ctx.schema_store,
        },
    )


def _build_explore_use_case(ctx: AppContext) -> ExploreUseCase:
    """构造主动探索用例（`dbchat.usecases.exploration`）。"""
    factory = _load_factory(
        "dbchat.usecases.exploration",
        ("build_explore_use_case", "build_pipeline", "build_explorer"),
        what="探索用例",
    )
    return _call_factory(
        factory,
        ctx,
        {
            "settings": ctx.settings,
            "llm": ctx.llm,
            "registry": ctx.tools,
            "conversations": ctx.conversations,
            "clarifications": ctx.clarifications,
            "explorations": ctx.explorations,
            "rag": ctx.rag,
            "schema_store": ctx.schema_store,
        },
    )


def _load_factory(module_name: str, candidates: tuple[str, ...], *, what: str) -> Any:
    """导入用例模块并取工厂。

    用例实现在并行开发中，这里**惰性导入**：模块还不存在时 API 层照常可导入、
    可测试（测试用 `dependency_overrides` 注入假实现）。真的在请求里用到却
    没实现时，给 503 与一句能照着修的报错，而不是把 ImportError 抛到浏览器。
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        logger.error("%s 尚未实现: 无法导入 %s", what, module_name)
        raise HTTPException(
            status_code=503, detail=f"{what}尚未实现（{module_name} 不存在），请稍后再试"
        ) from exc
    for name in candidates:
        factory = getattr(module, name, None)
        if callable(factory):
            return factory
    logger.error("%s 的模块 %s 中没有工厂 %s", what, module_name, "/".join(candidates))
    raise HTTPException(
        status_code=503,
        detail=f"{what}尚未实现（{module_name} 里没有 {'/'.join(candidates)}）",
    )


def _call_factory(factory: Callable[..., Any], ctx: AppContext, kwargs: dict[str, Any]) -> Any:
    """按工厂签名传参。

    两种约定都支持：

    * `factory(ctx)` —— 用例自己从 `AppContext` 取协作对象（首选，见
      `usecases.chat.build_chat_use_case` 的 docstring）；
    * `factory(**kwargs)` —— 显式传参，按签名过滤：不接受的键不传，
      带 `**kwargs` 的原样转发（如 `build_pipeline(**)`）。
    """
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内建/C 实现
        return factory(ctx)

    values = list(params.values())
    positional = [
        item
        for item in values
        if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) == 1 and not any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in values
    ):
        return factory(ctx)

    if not any(item.kind is inspect.Parameter.VAR_KEYWORD for item in values):
        kwargs = {key: value for key, value in kwargs.items() if key in params}
    try:
        return factory(**kwargs)
    except TypeError as exc:
        logger.error("构造用例失败（与接口层的传参约定不匹配）: %s", exc)
        raise HTTPException(status_code=503, detail=f"用例实现与接口层不匹配: {exc}") from exc


# --------------------------------------------------------------------- 依赖


def get_ctx(request: Request) -> AppContext:
    """取应用上下文（lifespan 构建，挂在 `app.state` 上）。"""
    ctx: AppContext | None = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - 只在 lifespan 未跑完时出现
        raise HTTPException(status_code=503, detail="应用尚未初始化完成")
    return ctx


#: 依赖的类型别名 —— 用 `Annotated` 而不是默认值，读起来更像"参数是什么"。
#: 顺带绕开 ruff 的 B008（默认值里调用函数），否则每个路由都要挂一串 noqa。
CtxDep = Annotated[AppContext, Depends(get_ctx)]


def get_chat_use_case(ctx: CtxDep) -> ChatUseCase:
    """聊天用例（测试用 `app.dependency_overrides[get_chat_use_case]` 注入假实现）。"""
    return ctx.chat_use_case()


def get_explore_use_case(ctx: CtxDep) -> ExploreUseCase:
    """探索用例（同上，可被注入替换）。"""
    return ctx.explore_use_case()


ChatUseCaseDep = Annotated[ChatUseCase, Depends(get_chat_use_case)]
ExploreUseCaseDep = Annotated[ExploreUseCase, Depends(get_explore_use_case)]


async def get_run_context(
    request: Request,
    ctx: CtxDep,
    profile_id: Annotated[str | None, Query(description="数据库配置 id")] = None,
    db_id: Annotated[str | None, Query(description="同上（前端沿用的参数名）")] = None,
    session_id: Annotated[str | None, Query(description="会话 id（前端生成）")] = None,
    table_filter: Annotated[
        str | None, Query(description="逗号分隔的表名，空表示不限")
    ] = None,
) -> AsyncIterator[RunContext]:
    """一次提问的全部可变状态（每个请求一个，退出即弃）。

    做四件事：

    1. 解析 profile —— 未知 id 在**流开始之前**就 404，不会让前端连上一条
       注定失败的流；
    2. 取/建会话（表筛选随之落库，下次不带参数也还在）；
    3. 借一次连接租约：连接不通就在这里变成 503（文案是中文的 `user_message`，
       不是驱动报文）；
    4. 组装 `RunContext` 并交给路由。

    关于租约的**持有范围**：这里只借到"建好 RunContext"为止。用例实现
    （`ChatUseCase.run(*, message, ctx)`）拿到的是 `RunContext`，协议里没有
    传租约的位置，所以它必须自己从注册表借一条。若这里持租约到流结束，
    同一 profile 的第二次 `acquire` 会永远等在同一把锁上 —— 自死锁。
    （工具的 `ToolContext.database()` 同样是"每次查询借还"，见
    `tools/registry.py`。）

    `RunContext.snapshot` 刻意留空：结构加载由用例在流内完成，用户因此能看到
    "加载数据库结构"这个阶段事件与心跳；放在这里会变成"第一个字节之前先卡住"，
    正是历史实现被前端超时兜住的那个行为。
    """
    pid = profile_id or db_id
    profile = await ctx.resolve_profile(pid)

    # 参数"没传"与"传了空值"要分开：没传表示沿用会话里已存的筛选，
    # 传了空串才表示"清掉筛选"。
    conversation = await ensure_conversation(
        ctx,
        session_id=session_id,
        profile_id=profile.id,
        table_filter=_parse_table_filter(table_filter),
        sync_filter=table_filter is not None,
    )
    scope = CatalogScope(
        frozenset(conversation.table_filter) if conversation.table_filter else None
    )

    run_ctx = RunContext(
        run_id=uuid.uuid4().hex[:12],
        profile=profile,
        scope=scope,
        conversation_id=conversation.id,
        history=await ctx.conversations.ahistory(conversation.id),
    )

    try:
        async with ctx.lease(profile):
            pass  # 借到就够：连接不通会在这里抛出 DbError
    except DbChatError as exc:
        logger.warning(
            "解析运行上下文时数据库不可用 (profile=%s): %s | %s",
            profile.id,
            exc.user_message,
            exc.detail or type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail=exc.user_message) from exc

    try:
        yield run_ctx
    finally:
        # 会话状态在请求结束时刷新一次 updated_at，供会话列表按活跃度排序。
        await ctx.conversations.atouch(conversation.id)


RunContextDep = Annotated[RunContext, Depends(get_run_context)]


# ------------------------------------------------------------------ 会话


def _parse_table_filter(raw: str | None) -> list[str] | None:
    """逗号分隔的表名 → 列表；空/全空白 → None（不限）。"""
    if not raw:
        return None
    names = [item.strip() for item in raw.split(",") if item.strip()]
    return names or None


async def ensure_conversation(
    ctx: AppContext,
    *,
    session_id: str | None,
    profile_id: str,
    table_filter: Sequence[str] | None = None,
    sync_filter: bool = True,
) -> Conversation:
    """取会话；没有就建一个，并把表筛选写进会话。

    Args:
        sync_filter: 是否把 `table_filter` 写进会话。只读的调用方（如
            `GET /table-filter`）要传 False —— 否则一次读取会把筛选清掉。

    会话 id 就是前端传来的 `session_id`（前端 `newId()` 与
    `uuid.uuid4().hex[:12]` 同构 —— 它本来就当作会话 id 用）。仓储的
    `create()` 只能自生成 id，而 `messages` 表对 `conversations` 有外键，
    必须真有一行，所以这里用 storage 的公开写事务补这一行；否则每轮都会新建
    一个会话，多轮上下文会整段丢掉。

    换了数据库（同一会话 id 指向别的 profile）时沿用历史语义：清掉旧消息、
    改归属，避免把 A 库的问答当成 B 库的上下文喂给模型。
    """
    if session_id:
        existing = await ctx.conversations.aget(session_id)
        if existing is not None:
            if existing.profile_id != profile_id:
                logger.info(
                    "会话 %s 从 %s 切到 %s，清空历史（与历史实现一致）",
                    session_id,
                    existing.profile_id,
                    profile_id,
                )
                await _rebind_conversation(
                    ctx,
                    session_id,
                    profile_id,
                    table_filter if sync_filter else existing.table_filter,
                )
                rebound = await ctx.conversations.aget(session_id)
                if rebound is not None:
                    return rebound
            elif sync_filter and list(existing.table_filter or ()) != list(table_filter or ()):
                await ctx.conversations.aset_table_filter(session_id, table_filter)
                return await ctx.conversations.aget(session_id) or existing
            else:
                return existing
        elif await _insert_conversation(ctx, session_id, profile_id, table_filter):
            created = await ctx.conversations.aget(session_id)
            if created is not None:
                return created

    return await ctx.conversations.acreate(profile_id, table_filter=table_filter)


async def _insert_conversation(
    ctx: AppContext,
    conversation_id: str,
    profile_id: str,
    table_filter: Sequence[str] | None,
) -> bool:
    """按调用方指定的 id 落一行会话（已存在则什么都不做）。"""
    payload = dump_json(list(table_filter)) if table_filter else None
    now = utc_now_str()

    def op(conn: sqlite3.Connection) -> bool:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO conversations
                (id, profile_id, title, created_at, updated_at, table_filter)
            VALUES (?, ?, '', ?, ?, ?)
            """,
            (conversation_id, profile_id, now, now, payload),
        )
        return bool(cursor.rowcount)

    return await ctx.storage.arun(op)


async def _rebind_conversation(
    ctx: AppContext,
    conversation_id: str,
    profile_id: str,
    table_filter: Sequence[str] | None,
) -> None:
    """把会话改挂到另一个 profile，并丢弃旧库的历史消息。"""
    payload = dump_json(list(table_filter)) if table_filter else None
    now = utc_now_str()

    def op(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute(
            "UPDATE conversations SET profile_id = ?, table_filter = ?, updated_at = ? "
            "WHERE id = ?",
            (profile_id, payload, now, conversation_id),
        )

    await ctx.storage.arun(op)


# ------------------------------------------------------------ 结构快照缓存


async def invalidate_snapshot(ctx: AppContext, profile_id: str) -> bool:
    """让某 profile 的结构缓存失效，返回是否真的清掉了东西。

    两条路径：

    * 有缓存对象（agent 运行时注入 `AppContext.schema_store`）→ 交给它；
    * 没有 → 直接删 `Settings.schema_cache_dir` 下属于该 profile 的缓存文件。
      文件名由 `dbchat.domain.schema.scope_cache_key` 决定（`{profile_id}:{scope}`），
      所以按 `{profile_id}*` 匹配即可覆盖全部 scope。
    """
    store = ctx.schema_store
    invalidate = getattr(store, "invalidate", None) if store is not None else None
    if callable(invalidate):
        try:
            result = invalidate(profile_id)
            if hasattr(result, "__await__"):
                await result
            return True
        except Exception as exc:  # noqa: BLE001 - 缓存失效失败只记日志，不影响调用方
            logger.warning(
                "结构快照缓存失效失败 (profile=%s): %s: %s", profile_id, type(exc).__name__, exc
            )
            return False

    def _clear() -> int:
        removed = 0
        for path in glob.glob(str(ctx.settings.schema_cache_dir / f"{profile_id}*")):
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("删除结构缓存文件失败 %s: %s", path, exc)
                continue
            removed += 1
        return removed

    removed = await anyio.to_thread.run_sync(_clear)
    logger.debug("清理结构缓存 (profile=%s): 删除 %d 个文件", profile_id, removed)
    return removed > 0
