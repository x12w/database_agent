"""文档检索层。

    chunker     markdown 切块
    indexer     FAISS 向量索引（不可用时退回 jieba 关键词检索）
    retriever   检索入口 + 管理员问答精确查询

用法::

    from dbchat.rag import get_indexer, retrieve_context

    await get_indexer().abuild()          # 建索引（编码在线程池里跑）
    context = await retrieve_context(q)   # 取相关文档
"""

from dbchat.rag.chunker import chunk_markdown
from dbchat.rag.indexer import (
    EMBEDDING_MODEL,
    IndexState,
    RAGIndexer,
    default_doc_paths,
    get_indexer,
)
from dbchat.rag.retriever import (
    aget_column_clarification,
    aretrieve_chunks,
    aretrieve_context,
    format_clarifications,
    get_column_clarification,
    retrieve_chunks,
    retrieve_context,
)

__all__ = [
    "EMBEDDING_MODEL",
    "IndexState",
    "RAGIndexer",
    "aget_column_clarification",
    "aretrieve_chunks",
    "aretrieve_context",
    "chunk_markdown",
    "default_doc_paths",
    "format_clarifications",
    "get_column_clarification",
    "get_indexer",
    "rag_indexer",
    "retrieve_chunks",
    "retrieve_context",
]


def __getattr__(name: str):
    """惰性提供 `rag_indexer` —— 兼容历史上的单例写法，但不在 import 阶段建索引。"""
    if name == "rag_indexer":
        return get_indexer()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
