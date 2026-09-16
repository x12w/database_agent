"""连接配置的增删改查，以及连接/缓存刷新。

历史实现在 `app/main.py` 与 `app/admin_app.py` 里各有一份（同一份数据两套
请求模型、口径还不一致），这里只留一份。
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from dbchat.api.live import list_tables
from dbchat.api.models import (
    DatabaseMutationResponse,
    DatabaseProfileView,
    DatabasesResponse,
    DatabaseStatusResponse,
    DeleteResponse,
    ProfileCreate,
    ProfileUpdate,
)
from dbchat.di import AppContext, CtxDep, invalidate_snapshot
from dbchat.domain.errors import DbChatError
from dbchat.domain.profile import DatabaseProfile, parse_dialect_kind

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/databases", tags=["databases"])

#: 探索报告里 agent 给出的建议块（历史格式，前端按行展示）。
_SUGGESTIONS_BLOCK = re.compile(r"```suggestions\s*\n(.*?)```", re.DOTALL)

#: 没有建议时的兜底 —— 至少让用户有东西可点。
_FALLBACK_SUGGESTIONS = ("介绍一下当前数据库架构", "数据库有哪些表？")


@router.get("", response_model=DatabasesResponse)
async def list_databases(ctx: CtxDep) -> DatabasesResponse:
    """全部连接配置（含"是否已探索""是否有活连接"）。"""
    profiles = await ctx.list_profiles()
    connected = ctx.connections.connected_profile_ids()
    views = [
        await _view(ctx, profile, connected=profile.id in connected) for profile in profiles
    ]
    current = profiles[0].id if profiles else None
    return DatabasesResponse(profiles=views, current=current)


@router.post("", response_model=DatabaseMutationResponse, status_code=201)
async def create_database(payload: ProfileCreate, ctx: CtxDep) -> DatabaseMutationResponse:
    """新增连接配置 —— 先自检连接，连不上就不写库。

    历史实现是"存了再说"，于是配置列表里会堆满连不上的库。
    """
    if not payload.name.strip() and not payload.database.strip():
        raise HTTPException(status_code=400, detail="请填写连接名称或数据库名")
    try:
        kind = parse_dialect_kind(payload.db_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    profile = DatabaseProfile(
        id=uuid.uuid4().hex[:12],
        name=payload.name.strip() or payload.database.strip(),
        host=payload.host,
        port=payload.port,
        user=payload.user,
        password=payload.password,
        database=payload.database,
        kind=kind,
        description=payload.description,
    )

    probe = await ctx.connections.probe(profile)
    if not probe.success:
        raise HTTPException(status_code=400, detail=probe.message)

    await ctx.profiles.aupsert(profile)
    logger.info("新增连接配置 %s (%s)", profile.id, profile.redacted().host)
    return DatabaseMutationResponse(
        message=probe.message, profile=await _view(ctx, profile, connected=False)
    )


@router.put("/{profile_id}", response_model=DatabaseMutationResponse)
async def update_database(
    profile_id: str, payload: ProfileUpdate, ctx: CtxDep
) -> DatabaseMutationResponse:
    """修改连接配置（只改传了的字段）。"""
    profile = await ctx.resolve_profile(profile_id)
    changes = payload.model_dump(exclude_none=True)
    if "db_type" in changes:
        try:
            changes["kind"] = parse_dialect_kind(str(changes.pop("db_type")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated = profile.model_copy(update=changes)
    probe = await ctx.connections.probe(updated)
    if not probe.success:
        raise HTTPException(status_code=400, detail=probe.message)

    await ctx.profiles.aupsert(updated)
    # 连接参数变了：把已建立的连接丢掉，下一次请求按新参数重连。
    await ctx.connections.close(profile_id)
    await invalidate_snapshot(ctx, profile_id)
    logger.info("更新连接配置 %s", profile_id)
    return DatabaseMutationResponse(
        message=probe.message, profile=await _view(ctx, updated, connected=False)
    )


@router.delete("/{profile_id}", response_model=DeleteResponse)
async def delete_database(profile_id: str, ctx: CtxDep) -> DeleteResponse:
    """删除连接配置，并清掉它的连接与探索记录。"""
    if not await ctx.profiles.adelete(profile_id):
        raise HTTPException(status_code=404, detail=f"数据库配置不存在: {profile_id}")
    await ctx.connections.close(profile_id)
    await ctx.explorations.adelete(profile_id)
    await invalidate_snapshot(ctx, profile_id)
    logger.info("删除连接配置 %s", profile_id)
    return DeleteResponse(message="连接配置已删除")


@router.get("/{profile_id}/status", response_model=DatabaseStatusResponse)
async def database_status(
    profile_id: str,
    ctx: CtxDep,
    refresh: Annotated[bool, Query(description="先丢掉连接再重新读取")] = False,
) -> DatabaseStatusResponse:
    """连接状态与库结构概览。

    连不上库时**仍然返回 200**，只把 `tables` 留空 —— 前端要能显示"未连接"，
    而不是把整页变成错误提示。
    """
    profile = await ctx.resolve_profile(profile_id)
    if refresh:
        await ctx.connections.close(profile_id)
        await invalidate_snapshot(ctx, profile_id)

    explored = await ctx.explorations.ais_explored(profile_id)
    report = await ctx.explorations.areport(profile_id)
    explored_at = ""
    record = await ctx.explorations.aget(profile_id)
    if record is not None:
        explored_at = record.created_at

    table_names: list[str] = []
    try:
        table_names = [info.name for info in await list_tables(ctx, profile)]
    except DbChatError as exc:
        logger.warning(
            "读取表清单失败 (profile=%s): %s | %s",
            profile.id,
            exc.user_message,
            exc.detail or type(exc).__name__,
        )

    return DatabaseStatusResponse(
        profile_id=profile_id,
        name=profile.display_name,
        database=profile.database,
        db_type=profile.kind.value,
        explored=explored,
        table_count=len(table_names),
        tables=table_names,
        suggestions=_suggestions(report, table_names),
        explored_at=explored_at,
        report=report,
    )


@router.post("/{profile_id}/refresh", response_model=DatabaseMutationResponse)
async def refresh_database(profile_id: str, ctx: CtxDep) -> DatabaseMutationResponse:
    """刷新连接：丢掉已建立的连接、让结构缓存失效，然后重新读一次表清单。"""
    profile = await ctx.resolve_profile(profile_id)
    await ctx.connections.close(profile_id)
    await invalidate_snapshot(ctx, profile_id)

    try:
        tables = await list_tables(ctx, profile)
    except DbChatError as exc:
        raise HTTPException(status_code=503, detail=exc.user_message) from exc

    return DatabaseMutationResponse(
        message=f"连接已刷新（{len(tables)} 张表）",
        profile=await _view(ctx, profile, connected=False),
        table_count=len(tables),
    )


@router.post("/{profile_id}/rebuild-cache", response_model=DatabaseMutationResponse)
async def rebuild_cache(
    profile_id: str,
    ctx: CtxDep,
    session_id: Annotated[str | None, Query(description="按该会话的表筛选重建")] = None,
) -> DatabaseMutationResponse:
    """重建结构缓存：丢掉连接、让缓存失效、（可选）按会话的表筛选重建。

    同时清掉探索记录（与历史实现一致）—— 重建缓存意味着结构认知作废，
    `explored` 应当回到 False，让 agent 重新探索而不是继续用旧结论。
    """
    profile = await ctx.resolve_profile(profile_id)
    await ctx.connections.close(profile_id)

    scope_note = "全部表"
    if session_id:
        conversation = await ctx.conversations.aget(session_id)
        if conversation is not None and conversation.table_filter:
            scope_note = f"筛选 {len(conversation.table_filter)} 张表"

    invalidated = await invalidate_snapshot(ctx, profile_id)
    await ctx.explorations.adelete(profile_id)

    try:
        tables = await list_tables(ctx, profile)
    except DbChatError as exc:
        raise HTTPException(status_code=503, detail=exc.user_message) from exc

    logger.info(
        "重建缓存 %s（%s，缓存层%s）",
        profile_id,
        scope_note,
        "已失效" if invalidated else "未提供",
    )
    return DatabaseMutationResponse(
        message=f"缓存已重建（{scope_note}，共 {len(tables)} 张表），探索记录已清除",
        profile=await _view(ctx, profile, connected=False),
        table_count=len(tables),
    )


async def _view(ctx: AppContext, profile: DatabaseProfile, *, connected: bool) -> DatabaseProfileView:
    """连接配置 → 展示形态（不含密码）。"""
    return DatabaseProfileView(
        id=profile.id,
        name=profile.display_name,
        host=profile.host,
        port=profile.port,
        user=profile.user,
        database=profile.database,
        db_type=profile.kind.value,
        description=profile.description,
        explored=await ctx.explorations.ais_explored(profile.id),
        connected=connected,
    )


def _suggestions(report: str, tables: list[str]) -> list[str]:
    """示例问题：优先用探索报告里 agent 给出的建议块，否则按表名兜底。"""
    match = _SUGGESTIONS_BLOCK.search(report or "")
    if match:
        items = [line.strip() for line in match.group(1).strip().splitlines() if line.strip()]
        if len(items) >= 2:
            return items[:5]

    suggestions = list(_FALLBACK_SUGGESTIONS)
    suggestions.extend(f"查询{t}表中有哪些数据" for t in tables[:3])
    return suggestions[:5]
