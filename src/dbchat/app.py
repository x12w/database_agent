"""FastAPI 应用装配。

历史实现是**两个应用、两个 uvicorn 进程**（`app/main.py` 跑 8000、
`app/admin_app.py` 跑 8001，由 `run.sh` 分别拉起），管理面板是另一份静态页，
`/api/admin/*` 在两边各实现了一遍。v4 只有一个应用：`/admin` 是 SPA 的一条
路由，`/api/admin/*` 是它背后的接口。

生命周期只有一处：`lifespan` 建目录、开 SQLite 库（跑迁移）、构建 `AppContext`
并挂到 `app.state` 上；请求通过 `di.get_ctx` 取用，模块级没有可变状态。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from dbchat.api import build_router
from dbchat.api.failures import ApiFailure, single_frame
from dbchat.config import Settings, get_settings
from dbchat.di import AppContext, build_context
from dbchat.domain.errors import DbChatError, DbError
from dbchat.events.sse import SSE_HEADERS

logger = logging.getLogger(__name__)

#: `web/dist` 不存在时给一句话，而不是让浏览器看到 404 的 JSON。
_MISSING_FRONTEND_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>dbchat</title></head>
<body style="font-family: system-ui, sans-serif; margin: 3rem;">
<h1>前端尚未构建</h1>
<p>请先构建前端：<code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code>，
或直接运行 <code>./run.sh</code>（它会自动构建）。</p>
<p>开发时也可以让 Vite 提供前端：<code>cd web &amp;&amp; npm run dev</code>，
它会把 <code>/api</code> 代理到本服务。</p>
</body></html>
"""


def create_app(
    settings: Settings | None = None,
    *,
    context: AppContext | None = None,
    build_rag: bool = True,
) -> FastAPI:
    """构建应用。

    Args:
        settings: 配置；不传用进程内单例（`.env`）。
        context: 直接注入应用上下文（测试用；不传则按 settings 构建）。
        build_rag: 是否在后台构建 RAG 索引。测试里关掉 —— 它要加载嵌入模型。
    """
    s = settings or get_settings()
    ctx = context or build_context(s)

    app = FastAPI(
        title="dbchat · 智能查询助手",
        description="用中文提问，Agent 自主查库并回答",
        version="0.4.0",
        lifespan=_lifespan(ctx, build_rag=build_rag),
    )
    # lifespan 里还会再挂一次；这里先挂，是为了让不跑 lifespan 的场景
    # （httpx.ASGITransport 直接打 ASGI 应用）也能取到上下文。
    app.state.ctx = ctx

    if s.debug:
        # 只有开发模式放开跨域：Vite dev server（5173）要直连这个服务。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(build_router())
    _mount_frontend(app, s)
    _install_error_handlers(app)
    return app


# ------------------------------------------------------------------ 生命周期


def _lifespan(ctx: AppContext, *, build_rag: bool):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = ctx.settings
        settings.ensure_dirs()
        app.state.ctx = ctx

        # 打开 SQLite 库并跑迁移。放在线程里执行：建库/迁移是磁盘 IO。
        await anyio.to_thread.run_sync(ctx.storage.migrate)

        profiles = await ctx.list_profiles()
        logger.info(
            "已加载 %d 个数据库配置，默认库: %s",
            len(profiles),
            profiles[0].display_name if profiles else "（无）",
        )
        if ctx.llm.available:
            logger.info("模型服务可用 (provider=%s, model=%s)", ctx.llm.provider, ctx.llm.model)
        else:
            logger.warning("未配置模型服务，将只能回答结构类问题（受限模式）")

        _warn_missing_runtime()

        if build_rag:
            _start_rag_build(ctx)

        # 实际监听地址以 uvicorn 自己那行为准（命令行参数可覆盖这里的配置值）。
        logger.info("配置的监听地址: %s:%s（实际监听见 uvicorn 日志）", settings.host, settings.port)
        try:
            yield
        finally:
            logger.info("关闭连接与本地存储…")
            await ctx.aclose()

    return lifespan


def _warn_missing_runtime() -> None:
    """用例实现（agent 运行时，并行开发中）还没落地时，启动日志里说清楚。

    这比等到用户提问才 503 要好排查得多。
    """
    for module_name, what in (
        ("dbchat.usecases.chat", "聊天用例"),
        ("dbchat.usecases.exploration", "探索用例"),
    ):
        if importlib.util.find_spec(module_name) is None:
            logger.warning("%s 尚未实现（%s 缺失），相关接口将返回 503", what, module_name)


def _start_rag_build(ctx: AppContext) -> None:
    """后台构建 RAG 索引（模型下载可能很慢或不可达，不能挡住启动）。"""

    async def build() -> None:
        try:
            await ctx.rag.abuild()
        except Exception as exc:  # noqa: BLE001 - 索引不可用不该让服务起不来
            logger.warning("RAG 索引构建失败（非致命，将退回关键词检索）: %s: %s", type(exc).__name__, exc)
        else:
            state = ctx.rag.state
            logger.info(
                "RAG 索引就绪（%d 个片段，向量检索=%s）", state.size, not state.fallback
            )

    task = asyncio.create_task(build())
    ctx.background_tasks.add(task)
    task.add_done_callback(ctx.background_tasks.discard)


# ------------------------------------------------------------ 前端与静态资源


def _mount_frontend(app: FastAPI, settings: Settings) -> None:
    """挂载构建产物 + SPA 兜底。

    约定：`/api/*` 只认已经注册的路由，**不会**回落到 index.html ——
    否则前端拼错一个接口名会拿到一整页 HTML，报错信息完全对不上。
    """
    dist = settings.web_dist_dir
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    else:
        logger.info("未找到前端构建产物 %s（开发模式下由 Vite dev server 提供）", assets)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")

        root = dist.resolve()
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)

        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html")
        return HTMLResponse(_MISSING_FRONTEND_HTML, status_code=404)


# ------------------------------------------------------------------ 异常兜底


def _install_error_handlers(app: FastAPI) -> None:
    """把领域异常翻译成中文文案。

    走到这里的通常是**非流式**路由（流式路由在自己的收尾事件里处理异常）。
    原始细节只进日志，响应里只有 `user_message`。
    """

    @app.exception_handler(ApiFailure)
    async def _api_failure(request: Request, exc: ApiFailure) -> StreamingResponse:
        """入参不合法 → 一帧 `run.failed`（只由 SSE 路由的依赖抛出）。"""
        logger.info("入参被拒绝 %s: %s", request.url.path, exc.message)
        return StreamingResponse(
            _one_frame(single_frame(exc)),
            media_type="text/event-stream",
            headers=dict(SSE_HEADERS),
        )

    @app.exception_handler(DbChatError)
    async def _domain_error(request: Request, exc: DbChatError) -> JSONResponse:
        status = 503 if isinstance(exc, DbError) else 500
        logger.warning(
            "%s %s 失败: %s | %s",
            request.method,
            request.url.path,
            exc.user_message,
            exc.detail or type(exc).__name__,
        )
        return JSONResponse(status_code=status, content={"detail": exc.user_message})


async def _one_frame(frame: str) -> AsyncIterator[str]:
    """把一帧已经编好的 SSE 文本包成流（异常处理器用）。"""
    yield frame
