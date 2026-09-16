"""管理面板接口：管理员问答闭环 + 主动探索。

历史实现在 `app/main.py` 与 `app/admin_app.py` 里各实现了一遍（两套请求模型、
`profile_id` 的取值口径还不一样：一边必填、一边回落到"当前连接"），
这里只留一份，并且**没有"当前连接"可回落** —— 缺 profile 时按 question_id
在所有 profile 里定位，定不到就 404，绝不猜。
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from dbchat.api.models import (
    AdminMutationResponse,
    AdminQuestionsResponse,
    AdminQuestionView,
    AnswerRequest,
    IgnoreRequest,
)
from dbchat.api.sse import explore_stream_response
from dbchat.di import AppContext, CtxDep, ExploreUseCaseDep
from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.schema import CatalogScope
from dbchat.storage import (
    STATUS_ANSWERED,
    STATUS_IGNORED,
    STATUS_PENDING,
    Clarification,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/questions", response_model=AdminQuestionsResponse)
async def list_questions(
    ctx: CtxDep,
    status: Annotated[
        str | None, Query(description="pending / answered / ignored / all")
    ] = None,
    profile_id: Annotated[str | None, Query(description="缺省用默认库")] = None,
) -> AdminQuestionsResponse:
    """管理员问答列表。"""
    profile = await ctx.resolve_profile(profile_id)

    if status == STATUS_PENDING:
        entries = await ctx.clarifications.aget_pending(profile.id)
    elif status == STATUS_ANSWERED:
        entries = await ctx.clarifications.aget_all_answered(profile.id)
    elif status == STATUS_IGNORED:
        entries = [
            item
            for item in await ctx.clarifications.aget_all(profile.id)
            if item.status == STATUS_IGNORED
        ]
    else:
        entries = await ctx.clarifications.aget_all(profile.id)

    return AdminQuestionsResponse(
        questions=[_view(entry) for entry in entries],
        pending_count=await ctx.clarifications.aget_pending_count(profile.id),
        total_count=len(entries),
        profile_id=profile.id,
    )


@router.post("/questions/{question_id}/answer", response_model=AdminMutationResponse)
async def answer_question(
    question_id: str, payload: AnswerRequest, ctx: CtxDep
) -> AdminMutationResponse:
    """回答一个待确认问题。

    答案会同时进入 RAG 语料（`build_rag_document`），所以回答后要重建索引 ——
    索引构建失败不影响"答案已保存"这件事，只记日志。
    """
    answer = payload.answer.strip()
    if not answer:
        raise _bad_request("答案不能为空")

    profile_id = await _locate_question(ctx, question_id, payload.profile_id)
    if not await ctx.clarifications.aanswer_question(profile_id, question_id, answer):
        raise _not_found(question_id)

    await _reindex_admin_docs(ctx, profile_id)
    logger.info("管理员已回答问题 %s (profile=%s)", question_id, profile_id)
    return AdminMutationResponse(
        question_id=question_id, profile_id=profile_id, message="答案已保存并索引到知识库"
    )


@router.post("/questions/{question_id}/ignore", response_model=AdminMutationResponse)
async def ignore_question(
    question_id: str, ctx: CtxDep, payload: IgnoreRequest | None = None
) -> AdminMutationResponse:
    """忽略一个待确认问题（不回答，也不进知识库）。"""
    profile_id = await _locate_question(
        ctx, question_id, payload.profile_id if payload else None
    )
    if not await ctx.clarifications.aignore_question(profile_id, question_id):
        raise _not_found(question_id)

    logger.info("管理员忽略了问题 %s (profile=%s)", question_id, profile_id)
    return AdminMutationResponse(
        question_id=question_id, profile_id=profile_id, message="问题已忽略"
    )


async def _explore_profile(
    ctx: CtxDep,
    db_id: Annotated[str | None, Query(description="数据库配置 id")] = None,
    profile_id: Annotated[
        str | None, Query(description="同上（管理面板早期参数名）")
    ] = None,
) -> DatabaseProfile:
    """先解析 profile 再解析用例：库不存在时给 404，而不是"用例未实现"的 503。"""
    return await ctx.resolve_profile(db_id or profile_id)


#: 探索接口的 profile 依赖（先解析库、再解析用例，见 `_explore_profile`）。
ExploreProfile = Annotated[DatabaseProfile, Depends(_explore_profile)]


@router.get("/explore-database")
async def explore_database(
    request: Request,
    profile: ExploreProfile,
    ctx: CtxDep,
    use_case: ExploreUseCaseDep,
    session_id: Annotated[str | None, Query(description="按该会话的表筛选探索")] = None,
) -> StreamingResponse:
    """主动探索数据库（SSE）。

    事件是探索通道的契约（`dbchat.events.explore`，5 种 `explore.*`）——
    这条通道的读者是管理员，表名、列名、样本值对他们都是必要信息。
    """
    tables: frozenset[str] | None = None
    if session_id:
        conversation = await ctx.conversations.aget(session_id)
        if conversation is not None and conversation.table_filter:
            tables = frozenset(conversation.table_filter)

    logger.info("开始探索 profile=%s（表筛选：%s）", profile.id, len(tables) if tables else "不限")
    return explore_stream_response(
        request,
        use_case.run(profile=profile, scope=CatalogScope(tables)),
        run_id=uuid.uuid4().hex[:12],
        debug=ctx.settings.debug,
    )


def _view(entry: Clarification) -> AdminQuestionView:
    return AdminQuestionView(
        question_id=entry.question_id,
        table_name=entry.table_name,
        column_name=entry.column_name,
        question=entry.question,
        sample_values=[str(value) for value in entry.sample_values],
        answer=entry.answer,
        status=entry.status,
        created_at=entry.created_at,
        answered_at=entry.answered_at,
        profile_id=entry.profile_id,
    )


async def _locate_question(ctx: AppContext, question_id: str, profile_id: str | None) -> str:
    """确定问题属于哪个 profile。

    给了 profile_id 就用它（问题必须在那个 profile 下存在）；没给就在所有
    profile 里找 —— 前端在未选库时会传 null，猜一个库去答会把答案挂错地方。
    """
    if profile_id:
        if await ctx.clarifications.aget(profile_id, question_id) is None:
            raise _not_found(question_id)
        return profile_id

    for candidate in await ctx.clarifications.aget_all_profile_ids():
        if await ctx.clarifications.aget(candidate, question_id) is not None:
            return candidate
    raise _not_found(question_id)


async def _reindex_admin_docs(ctx: AppContext, profile_id: str) -> None:
    """把管理员问答并入 RAG 索引（失败只记日志）。"""
    try:
        admin_doc = await ctx.clarifications.abuild_rag_document(profile_id)
        await ctx.rag.abuild_admin_index(admin_doc, profile_id)
    except Exception as exc:  # noqa: BLE001 - 索引失败不该让"答案已保存"变成失败
        logger.warning(
            "重建管理员问答索引失败 (profile=%s): %s: %s", profile_id, type(exc).__name__, exc
        )


def _not_found(question_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"问题不存在: {question_id}")


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)
