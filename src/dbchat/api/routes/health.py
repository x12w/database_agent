"""健康检查。"""

from __future__ import annotations

from fastapi import APIRouter

from dbchat.api.models import HealthResponse
from dbchat.di import CtxDep

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthResponse)
async def health(ctx: CtxDep) -> HealthResponse:
    """服务自检。

    刻意**不碰数据库**：这个接口是给探活/前端首屏用的，一次连接建立就要几百
    毫秒。`database` 反映的是"当前有没有活连接"（连接由请求按需借出并保留复用），
    而不是"现在能不能连上"——后者由 `/api/databases/{id}/status` 回答。

    `schema_tables` 在结构缓存层尚未落地时为 0（前端把它当"未知"处理）。
    """
    connected = ctx.connections.connected_profile_ids()
    db_name: str | None = None
    if connected:
        profile = await ctx.profiles.aget(sorted(connected)[0])
        db_name = profile.database if profile is not None else None

    return HealthResponse(
        status="ok",
        database=bool(connected),
        db_name=db_name,
        llm_available=ctx.llm.available,
        rag_ready=ctx.rag.ready,
        schema_tables=0,
    )
