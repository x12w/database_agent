"""HTTP Basic (browser) / Bearer (API) authentication, with user/admin roles."""

from __future__ import annotations

import base64
import binascii
import secrets

from fastapi import HTTPException, Request


async def authorize(request: Request) -> str:
    settings = request.app.state.ctx.settings
    if not settings.auth_admin_token:
        raise HTTPException(503, "请先配置 AUTH_ADMIN_TOKEN，再启动服务")
    if settings.auth_user_token == settings.auth_admin_token:
        raise HTTPException(503, "普通用户与管理员必须使用不同的令牌")
    # Browsers cache Basic credentials, including on cross-site requests.
    origin = request.headers.get("origin")
    allowed_origins = {str(request.base_url).rstrip("/")}
    if settings.debug:
        allowed_origins.update({"http://localhost:5173", "http://127.0.0.1:5173"})
    if request.url.path.startswith("/api/") and (
        request.headers.get("sec-fetch-site") == "cross-site"
        or (origin is not None and origin not in allowed_origins)
    ):
        raise HTTPException(403, "不允许跨站请求")
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    username = ""
    token = value
    if scheme.lower() == "basic":
        try:
            username, _, token = base64.b64decode(value, validate=True).decode().partition(":")
        except (ValueError, UnicodeError, binascii.Error):
            token = ""
    elif scheme.lower() != "bearer":
        token = ""

    role = ""
    if (
        token
        and (not username or username == "admin")
        and secrets.compare_digest(token.encode(), settings.auth_admin_token.encode())
    ):
        role = "admin"
    elif (
        token
        and settings.auth_user_token
        and (not username or username == "user")
        and secrets.compare_digest(token.encode(), settings.auth_user_token.encode())
    ):
        role = "user"
    if not role:
        raise HTTPException(
            401, "请登录", headers={"WWW-Authenticate": 'Basic realm="dbchat", charset="UTF-8"'}
        )

    path = request.url.path.rstrip("/")
    admin_only = (
        path == "/admin"
        or path.startswith("/api/admin")
        or (path.startswith("/api/databases") and request.method not in {"GET", "HEAD"})
        or (
            path.startswith("/api/databases")
            and request.query_params.get("refresh", "").lower() in {"1", "true", "yes", "on"}
        )
    )
    if admin_only and role != "admin":
        raise HTTPException(403, "此操作需要管理员权限")
    return role
