"""检索入口。

对上层只暴露"给我相关文档"和"这张表的这一列管理员是怎么说的"两件事。
同步版本给脚本/测试用，`a*` 版本给事件循环用（编码与 SQLite 都在线程池里）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from functools import partial
from typing import Any, Protocol

import anyio

from dbchat.rag.indexer import RAGIndexer, get_indexer

logger = logging.getLogger(__name__)


class ClarificationLookup(Protocol):
    """检索层需要的管理员问答查询能力（避免硬依赖 storage 的具体实现）。"""

    def get_clarification(
        self, profile_id: str, table_name: str, column_name: str = ""
    ) -> Sequence[Any]: ...


def retrieve_chunks(
    query: str,
    top_k: int | None = None,
    *,
    indexer: RAGIndexer | None = None,
) -> list[tuple[str, float]]:
    """检索相关文档片段，返回 `[(片段, 分数), ...]`。索引未就绪时返回空。"""
    active = indexer or get_indexer()
    if not active.ready:
        return []
    return active.search(query, top_k)


async def aretrieve_chunks(
    query: str,
    top_k: int | None = None,
    *,
    indexer: RAGIndexer | None = None,
) -> list[tuple[str, float]]:
    """`retrieve_chunks` 的异步版本。"""
    return await anyio.to_thread.run_sync(partial(retrieve_chunks, query, top_k, indexer=indexer))


def retrieve_context(
    query: str,
    top_k: int | None = None,
    *,
    indexer: RAGIndexer | None = None,
) -> str:
    """检索并把片段拼成可直接塞进提示词的 markdown。"""
    chunks = retrieve_chunks(query, top_k, indexer=indexer)
    if not chunks:
        return ""

    parts = ["## 相关文档内容\n"]
    for i, (chunk, score) in enumerate(chunks):
        parts.append(f"### 文档片段 {i + 1}（相关度: {score:.2f}）")
        parts.append(chunk)
        parts.append("")
    return "\n".join(parts)


async def aretrieve_context(
    query: str,
    top_k: int | None = None,
    *,
    indexer: RAGIndexer | None = None,
) -> str:
    """`retrieve_context` 的异步版本。"""
    chunks = await aretrieve_chunks(query, top_k, indexer=indexer)
    if not chunks:
        return ""
    parts = ["## 相关文档内容\n"]
    for i, (chunk, score) in enumerate(chunks):
        parts.append(f"### 文档片段 {i + 1}（相关度: {score:.2f}）")
        parts.append(chunk)
        parts.append("")
    return "\n".join(parts)


def get_column_clarification(
    profile_id: str,
    table_name: str,
    column_name: str = "",
    *,
    repository: ClarificationLookup | None = None,
) -> str:
    """取某个表/列的管理员说明（精确匹配，比语义检索更可靠）。

    `column_name` 留空表示要该表的全部说明 —— 与历史实现一致。

    Returns:
        若干行「- `表`.`列`: 答案」；没有匹配返回空字符串。
    """
    if not profile_id:
        return ""
    repo = repository or _default_clarification_repository()
    if repo is None:
        return ""
    try:
        entries = repo.get_clarification(profile_id, table_name, column_name)
    except Exception as exc:
        logger.warning("查询管理员说明失败: %s", exc, exc_info=True)
        return ""
    return format_clarifications(entries)


async def aget_column_clarification(
    profile_id: str,
    table_name: str,
    column_name: str = "",
    *,
    repository: ClarificationLookup | None = None,
) -> str:
    """`get_column_clarification` 的异步版本（SQLite 查询在线程池里跑）。"""
    return await anyio.to_thread.run_sync(
        partial(
            get_column_clarification,
            profile_id,
            table_name,
            column_name,
            repository=repository,
        )
    )


def format_clarifications(entries: Sequence[Any]) -> str:
    """把问答条目渲染成提示词片段。"""
    parts: list[str] = []
    for entry in entries:
        column = getattr(entry, "column_name", "") or ""
        table = getattr(entry, "table_name", "") or ""
        answer = getattr(entry, "answer", "") or ""
        if column:
            parts.append(f"- `{table}`.`{column}`: {answer}")
        else:
            parts.append(f"- `{table}`: {answer}")
    return "\n".join(parts)


def _default_clarification_repository() -> ClarificationLookup | None:
    """默认走 SQLite；导入失败（例如 storage 尚未就绪）只记日志。"""
    try:
        from dbchat.storage.clarifications import get_clarification_repository

        return get_clarification_repository()
    except Exception as exc:
        logger.warning("管理员问答仓储不可用: %s", exc, exc_info=True)
        return None


__all__ = [
    "ClarificationLookup",
    "aget_column_clarification",
    "aretrieve_chunks",
    "aretrieve_context",
    "format_clarifications",
    "get_column_clarification",
    "retrieve_chunks",
    "retrieve_context",
]
