"""Administrator-only desktop configuration and explicit model connection test."""

from __future__ import annotations

import asyncio
import time

import anyio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from dbchat import preferences
from dbchat.di import CtxDep
from dbchat.llm.client import LLMClient
from dbchat.llm.messages import user_message

router = APIRouter(prefix="/api/admin/settings", tags=["settings"])


class Changes(BaseModel):
    values: dict = Field(default_factory=dict)
    revision: str = ""


@router.get("")
async def get_preferences(ctx: CtxDep) -> dict:
    try:
        return await anyio.to_thread.run_sync(preferences.view, ctx.settings)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.put("")
async def put_preferences(payload: Changes, ctx: CtxDep) -> dict:
    if not ctx.settings.desktop_mode:
        raise HTTPException(409, "请使用桌面应用保存运行设置")
    try:
        revision = await anyio.to_thread.run_sync(
            preferences.save, ctx.settings, payload.values, payload.revision
        )
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"revision": revision, "restart_required": True, "message": "设置已保存，重启后生效"}


@router.post("/test-model")
async def test_model(payload: Changes, ctx: CtxDep) -> dict:
    try:
        values = await anyio.to_thread.run_sync(preferences.candidate, ctx.settings, payload.values)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    settings = ctx.settings.model_copy(update=values)
    settings.llm_max_retries = 0
    client = LLMClient(settings)
    if not client.available:
        raise HTTPException(422, "请先填写 API 密钥；本地兼容服务可填写其要求的占位值")
    started = time.monotonic()
    try:
        async with asyncio.timeout(min(settings.llm_timeout_s, 30)):
            await client.complete(
                "You are a connection test assistant.",
                [user_message("Reply with OK only.")],
                max_tokens=16,
            )
    except Exception:
        # SDK error bodies can contain API keys or sensitive gateway details.
        raise HTTPException(
            502, "模型连接测试失败，请检查服务地址、协议、模型名和密钥，或稍后重试"
        ) from None
    finally:
        await client.aclose()
    return {
        "ok": True,
        "message": "模型连接成功",
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
