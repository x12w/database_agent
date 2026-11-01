"""Private desktop engine. No .env, fixed port, or manually managed login needed."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path

import uvicorn

from dbchat import preferences
from dbchat.app import create_app
from dbchat.config import Settings, configure_runtime


def desktop_settings(data_dir: Path, web_dir: Path, token: str) -> Settings:
    if len(token) < 32:
        raise ValueError("桌面会话凭据无效")
    data_dir = data_dir.resolve()
    # Pass every field explicitly: desktop settings must not inherit server env secrets.
    values = {key: field.default for key, field in Settings.model_fields.items()}
    values.update(
        db_name="",
        db_user="",
        rag_doc_path=data_dir / "knowledge.md",
        rag_structure_doc_path=data_dir / "structure.md",
    )
    values.update(preferences.load(data_dir / "preferences.json"))
    values.update(
        var_dir=data_dir,
        web_dist_dir=web_dir.resolve(),
        desktop_mode=True,
        host="127.0.0.1",
        port=0,
        debug=False,
        auth_admin_token=token,
        auth_user_token="",
    )
    return Settings(_env_file=None, **values)


async def serve(settings: Settings) -> None:
    configure_runtime(settings)
    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("本地引擎启动失败")
            await asyncio.sleep(0.02)
        print(json.dumps({"event": "ready", "port": sock.getsockname()[1]}), flush=True)
        await task


def main() -> None:
    parser = argparse.ArgumentParser(description="dbchat 桌面引擎（由桌面应用管理）")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--web-dir", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.pop("DBCHAT_DESKTOP_TOKEN", "")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    # The main process owns stdin. If it crashes, do not leave an orphan engine.
    def watch_parent() -> None:
        sys.stdin.buffer.read()
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=watch_parent, daemon=True).start()
    asyncio.run(serve(desktop_settings(args.data_dir, args.web_dir, token)))


if __name__ == "__main__":
    main()
