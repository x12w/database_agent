"""表清单、会话表筛选与图表产物。

表筛选从"两个模块级全局变量"（`schema_cache._active_table_filter`、
`agent_tools._active_table_filter`）改成**跟着会话走**：筛选条件存在会话行上，
随 `RunContext.scope` 传给 agent，不再有"谁最后设置谁生效"。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from dbchat.api.live import list_tables
from dbchat.api.models import TableFilterRequest, TableFilterResponse, TablesResponse
from dbchat.di import AppContext, CtxDep, ensure_conversation
from dbchat.domain.errors import DbChatError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["catalog"])


@router.get("/api/tables", response_model=TablesResponse)
async def tables(
    ctx: CtxDep,
    db_id: Annotated[str | None, Query(description="数据库配置 id，缺省用默认库")] = None,
) -> TablesResponse:
    """列出库里的**全部**表。

    刻意不查结构缓存：筛选界面必须看到每一张表，否则一旦筛过就再也选不回来。
    连不上库时返回 200 + `message`（前端在弹窗里展示这句话），与历史行为一致。
    """
    profile = await ctx.resolve_profile(db_id)
    try:
        return TablesResponse(tables=await list_tables(ctx, profile))
    except DbChatError as exc:
        logger.warning(
            "读取表清单失败 (profile=%s): %s | %s",
            profile.id,
            exc.user_message,
            exc.detail or type(exc).__name__,
        )
        return TablesResponse(tables=[], message=exc.user_message)


@router.get("/api/session/{session_id}/table-filter", response_model=TableFilterResponse)
async def get_table_filter(session_id: str, ctx: CtxDep) -> TableFilterResponse:
    """读取会话的表筛选条件（不会改动它）。"""
    conversation = await _session(ctx, session_id, sync=False)
    return TableFilterResponse(
        message="",
        table_filter=list(conversation.table_filter) if conversation.table_filter else None,
    )


@router.post("/api/session/{session_id}/table-filter", response_model=TableFilterResponse)
async def set_table_filter(
    session_id: str, payload: TableFilterRequest, ctx: CtxDep
) -> TableFilterResponse:
    """设置/清除会话的表筛选条件。`{"tables": null}` 表示不限。"""
    tables = [name for name in (payload.tables or []) if name.strip()]
    conversation = await _session(ctx, session_id, table_filter=tables or None, sync=True)
    if tables:
        return TableFilterResponse(
            message=f"已选择 {len(tables)} 张表",
            table_filter=list(conversation.table_filter or ()),
        )
    return TableFilterResponse(message="已清除表筛选，使用全部表", table_filter=None)


@router.get("/api/artifacts/{name}")
async def artifact(name: str, ctx: CtxDep) -> FileResponse:
    """图表等产物。

    历史实现从 `app/static/charts/` 提供，现在统一落在 `Settings.artifacts_dir`。
    名字只允许是纯文件名：路径分隔符与 `..` 一律拒绝，避免越出产物目录。
    """
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise HTTPException(status_code=404, detail="产物不存在")

    path = (ctx.settings.artifacts_dir / name).resolve()
    if not path.is_relative_to(ctx.settings.artifacts_dir.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="产物不存在")
    return FileResponse(path)


async def _session(
    ctx: AppContext,
    session_id: str,
    *,
    table_filter: list[str] | None = None,
    sync: bool,
):
    """取会话；没有就按默认库建一个（前端可能先设置筛选、再提问）。

    Args:
        sync: 是否把 `table_filter` 写进会话。读接口传 False —— 否则一次 GET
            就会把筛选清掉。
    """
    if not session_id.strip():
        raise HTTPException(status_code=400, detail="缺少会话 id")
    existing = await ctx.conversations.aget(session_id)
    if existing is not None:
        if sync:
            await ctx.conversations.aset_table_filter(session_id, table_filter)
            return await ctx.conversations.aget(session_id)
        return existing
    if not sync:
        raise HTTPException(status_code=404, detail="会话不存在")
    profile = await ctx.resolve_profile(None)
    return await ensure_conversation(
        ctx,
        session_id=session_id,
        profile_id=profile.id,
        table_filter=table_filter,
        sync_filter=sync,
    )
