"""接口层 —— FastAPI 路由与 SSE。

    models.py   请求 / 响应模型
    failures.py 异常 → 线上失败事件（中文文案）
    sse.py      心跳、断连取消、帧发送时机
    live.py     直接问数据库的小查询（表清单等）
    routes/     按资源分组的路由

全部路由由 `build_router()` 装配，应用装配在 `dbchat.app.create_app()`。
"""

from __future__ import annotations

from fastapi import APIRouter

from dbchat.api.routes import all_routers


def build_router() -> APIRouter:
    """把 `/api/*` 路由装进一个 router（顺序即注册顺序）。"""
    router = APIRouter()
    for sub in all_routers():
        router.include_router(sub)
    return router


__all__ = ["build_router"]
