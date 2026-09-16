"""`python -m dbchat` —— 启动唯一一个服务。

历史实现要起两个进程（8000 聊天 + 8001 管理面板，见旧 `run.sh`），
管理面板因此连不上主进程的连接状态。现在只有一个：`/admin` 是 SPA 的路由。
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

import uvicorn

from dbchat.config import get_settings

logger = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="dbchat",
        description="dbchat —— 自然语言数据库查询服务（MySQL / PostgreSQL / 达梦）",
    )
    parser.add_argument("--host", default=settings.host, help=f"监听地址（默认 {settings.host}）")
    parser.add_argument(
        "--port", type=int, default=settings.port, help=f"监听端口（默认 {settings.port}）"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=settings.debug,
        help="代码变更自动重启（默认跟随 .env 的 DEBUG）",
    )
    parser.add_argument(
        "--log-level",
        default="debug" if settings.debug else "info",
        choices=["trace", "debug", "info", "warning", "error", "critical"],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """启动服务（阻塞直到退出）。"""
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("启动 dbchat: http://%s:%s", args.host, args.port)
    uvicorn.run(
        "dbchat.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
