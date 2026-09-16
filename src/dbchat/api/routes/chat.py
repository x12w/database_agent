"""聊天主通道（SSE）。

路径与参数沿用前端已经用上的那一套：`message` / `session_id` / `db_id` /
`table_filter`（逗号分隔）。事件只有 `dbchat.events.chat.ChatEvent` 那 13 种。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from dbchat.api.failures import ApiFailure
from dbchat.api.sse import chat_stream_response
from dbchat.di import AppContext, ChatUseCaseDep, CtxDep, RunContext, RunContextDep
from dbchat.events.base import ErrorCode
from dbchat.events.chat import ChatEvent, RunFailed, RunFinished

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

#: 单条提问的字符上限。这一层只是把明显异常的输入挡在门口 ——
#: 真正的预算控制（轮次、token、总时长）在用例层。
MAX_MESSAGE_CHARS = 8000

#: 运行没能给出回答时写进历史的占位文本。
#: 失败也写一条（历史实现的 `_persist()` 同样如此）：漏写时下一轮模型看到的是
#: "问了一句没人回答"，会把上一轮的答案再复述一遍。
UNANSWERED_MARKER = "（系统：本轮未能给出回答）"


async def validated_message(
    run_ctx: RunContextDep,
    message: Annotated[str, Query(description="用户提问")] = "",
) -> str:
    """校验提问文本。

    做成依赖（而不是写在路由体里）是为了**顺序**：FastAPI 先解依赖再进路由体，
    于是"库不存在 → 404"和"提问为空 → run.failed"都会排在"用例是否就绪 → 503"
    之前。用户输错东西时不该先看到"服务未就绪"。
    """
    if not message.strip():
        raise ApiFailure(ErrorCode.VALIDATION, "请输入问题内容", run_id=run_ctx.run_id)
    if len(message) > MAX_MESSAGE_CHARS:
        raise ApiFailure(
            ErrorCode.VALIDATION,
            f"问题过长（上限 {MAX_MESSAGE_CHARS} 字），请精简后重试",
            run_id=run_ctx.run_id,
        )
    return message


#: 校验过的提问文本（依赖链上排在用例之前，见 `validated_message`）。
ValidatedMessage = Annotated[str, Depends(validated_message)]


@router.get("/api/chat/stream")
async def chat_stream(
    request: Request,
    message: ValidatedMessage,
    run_ctx: RunContextDep,
    ctx: CtxDep,
    use_case: ChatUseCaseDep,
) -> StreamingResponse:
    """流式回答一次提问。

    会话历史由这一层维护（用例只负责产出事件）：

    * 用户消息在起跑前落库 —— 它同时给会话起了个标题；
    * 回答在结束时落库；失败、取消也留一条占位回复。

    于是下一轮 `RunContext.history` 里每一问都有对应的一答 —— 缺了它，模型会
    把上一轮的答案再复述一遍（历史实现踩过这个坑）。
    """
    logger.info(
        "开始运行 run_id=%s profile=%s 会话=%s 历史=%d 条",
        run_ctx.run_id,
        run_ctx.profile.id,
        run_ctx.conversation_id,
        len(run_ctx.history),
    )
    await ctx.conversations.astore_message(run_ctx.conversation_id, "user", message)
    return chat_stream_response(
        request,
        _record_turn(use_case.run(message=message, ctx=run_ctx), ctx=ctx, run_ctx=run_ctx),
        run_id=run_ctx.run_id,
        debug=ctx.settings.debug,
    )


async def _record_turn(
    source: AsyncIterator[ChatEvent], *, ctx: AppContext, run_ctx: RunContext
) -> AsyncIterator[ChatEvent]:
    """把本轮的助手回复写回会话历史（用户消息已在起跑前写过了）。"""
    answered = False
    try:
        async for event in source:
            if isinstance(event, RunFinished):
                answered = True
                await _store(ctx, run_ctx, event.answer_markdown or UNANSWERED_MARKER)
            elif isinstance(event, RunFailed):
                answered = True
                await _store(ctx, run_ctx, UNANSWERED_MARKER)
            yield event
    finally:
        if not answered:
            # 走到这里说明运行被取消（客户端断开 / 关页面）。
            # 屏蔽取消：这是收尾写入，被打断会让历史里留下一句没人回答的提问。
            with anyio.CancelScope(shield=True):
                await _store(ctx, run_ctx, UNANSWERED_MARKER)


async def _store(ctx: AppContext, run_ctx: RunContext, text: str) -> None:
    """写一条助手消息；失败只记日志（不影响这一轮的结论）。"""
    try:
        await ctx.conversations.astore_message(
            run_ctx.conversation_id, "assistant", text.strip() or UNANSWERED_MARKER
        )
    except Exception as exc:  # noqa: BLE001 - 存历史失败不该影响本次回答
        logger.warning(
            "保存会话消息失败 (会话=%s): %s: %s",
            run_ctx.conversation_id,
            type(exc).__name__,
            exc,
        )
