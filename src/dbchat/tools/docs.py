"""文档检索与管理员问答工具。

`ask_admin` 的去重语义完全落在 `storage/clarifications.py` 上：已答过就直接
把历史答案回给模型（不重复提问），没问过才登记待确认。这套语义与历史实现
逐条对齐过，**不要**在这里另加判断 —— 多判一次就会出现同一列被反复提问，
或者该问的列被漏掉。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from dbchat.events.agent import AdminQuestionRaised
from dbchat.rag.retriever import aretrieve_chunks
from dbchat.storage.clarifications import get_clarification_repository
from dbchat.tools.registry import ToolContext, ToolOutcome

logger = logging.getLogger(__name__)

__all__ = [
    "ASK_ADMIN_DESCRIPTION",
    "AskAdminParams",
    "SearchDocsParams",
    "ask_admin",
    "search_documentation",
]


class SearchDocsParams(BaseModel):
    """文档检索入参。"""

    query: str = Field(description="关于数据库结构的搜索查询")


class AskAdminParams(BaseModel):
    """管理员提问入参。"""

    table_name: str = Field(description="表名")
    question: str = Field(description="清晰的问题，包含上下文和样本值")
    column_name: str = Field(default="", description="列名。整表级问题留空")
    samples: list[str] = Field(
        default_factory=list,
        description="该列的样本值（可选，帮助管理员判断）",
    )


SEARCH_DOCS_DESCRIPTION = (
    "搜索数据库文档和管理员说明以获取信息：表结构、列含义、编码值映射、业务规则等。"
    "管理员可能已经回答过关于某些列的说明 —— 对列含义不确定时应优先使用此工具搜索。"
    "返回最相关的文档片段和管理员说明。"
)

ASK_ADMIN_DESCRIPTION = (
    "向数据库管理员提问，获取模糊列的真实业务含义或编码值映射。"
    "仅在通过自主探索和 search_documentation 都无法确定列含义时使用。"
    "适用场景：列名是缩写/编码（如 F_STATUS、COL_003），"
    "且样本值是编码或缩写（如 '01'/'02'/'03'、'A'/'B'/'C'），无法仅从样本值推断含义。"
    "提问应清晰、包含上下文（样本值、你的推测）。"
    "不适用于：含义自明的列（如样本值本身就是中文部门名称、人名等）。"
    "工具会立即返回（不等待管理员回答），答案将在后续查询中通过 RAG 提供。"
)


async def search_documentation(params: SearchDocsParams, ctx: ToolContext) -> ToolOutcome:
    """检索文档库与管理员已答说明。"""
    query = (params.query or "").strip()
    if not query:
        return ToolOutcome.failure("未提供搜索查询", content="错误：未提供搜索查询。")

    chunks = await aretrieve_chunks(query)
    if not chunks:
        return ToolOutcome(
            ok=True,
            summary="文档中未找到相关信息",
            content="文档中未找到相关信息。",
            payload={"matches": 0},
        )

    parts: list[str] = []
    for index, (chunk, score) in enumerate(chunks, 1):
        parts.append(f"--- 文档片段 {index}（相关度: {score:.2f}）---")
        parts.append(chunk)
    return ToolOutcome(
        ok=True,
        summary=f"命中 {len(chunks)} 个文档片段",
        content="\n\n".join(parts),
        payload={"matches": len(chunks), "query": query},
    )


async def ask_admin(params: AskAdminParams, ctx: ToolContext) -> ToolOutcome:
    """向管理员提交一个问题（或取回历史答案）。"""
    table_name = (params.table_name or "").strip()
    question = (params.question or "").strip()
    if not table_name or not question:
        return ToolOutcome.failure("未提供表名或问题", content="错误：未提供表名或问题。")

    column_name = (params.column_name or "").strip()
    repository = ctx.clarifications or get_clarification_repository()
    profile_id = ctx.profile_id

    try:
        existing = await repository.aget_clarification(profile_id, table_name, column_name)
    except Exception as exc:  # noqa: BLE001 - 读失败不等于"没问过"，必须记日志
        logger.warning("查询历史问答失败: %s: %s", type(exc).__name__, exc, exc_info=True)
        return ToolOutcome.failure(
            "无法访问管理员问答记录",
            content=f"读取管理员问答记录时出错：{exc}",
            detail=f"{type(exc).__name__}: {exc}",
        )

    if existing:
        answers: list[str] = []
        for entry in existing:
            label = (
                f"`{entry.table_name}`.`{entry.column_name}`"
                if entry.column_name
                else f"`{entry.table_name}`"
            )
            answers.append(f"{label}: {entry.answer}")
        return ToolOutcome(
            ok=True,
            summary="管理员已说明过该列含义",
            content="管理员已说明过该列含义：\n" + "\n".join(answers),
            payload={"question_id": existing[0].question_id, "already_answered": True},
        )

    try:
        question_id = await repository.aadd_question(
            profile_id=profile_id,
            table_name=table_name,
            column_name=column_name,
            question=question,
            sample_values=list(params.samples or ()),
        )
    except Exception as exc:  # noqa: BLE001 - 保存失败要让模型知道，不能静默
        logger.warning("保存管理员问题失败: %s: %s", type(exc).__name__, exc, exc_info=True)
        return ToolOutcome.failure(
            "保存问题时出错",
            content=f"保存问题时出错: {exc}",
            detail=f"{type(exc).__name__}: {exc}",
        )

    return ToolOutcome(
        ok=True,
        summary="已提交给管理员确认",
        content=(
            f"已向管理员提交问题（ID: {question_id}）。"
            f"管理员回答后，后续查询将自动使用该信息。"
            f"当前查询请基于已有信息做出最佳努力的回答。"
        ),
        payload={"question_id": question_id, "table_name": table_name, "column_name": column_name},
        events=(
            AdminQuestionRaised(
                question_id=question_id,
                table=table_name,
                column=column_name,
                question=question,
                samples=tuple(params.samples or ()),
            ),
        ),
    )
