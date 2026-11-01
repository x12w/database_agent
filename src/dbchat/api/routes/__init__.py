"""路由模块。

每个模块暴露一个 `router`；装配在 `dbchat.api.build_router()`。
"""

from __future__ import annotations

from fastapi import APIRouter

from dbchat.api.routes import admin, catalog, chat, databases, health, preferences


def all_routers() -> list[APIRouter]:
    """全部路由（顺序即注册顺序：`/api/*` 必须排在 SPA 兜底之前）。"""
    return [health.router, databases.router, catalog.router, chat.router, admin.router, preferences.router]


__all__ = ["all_routers"]
