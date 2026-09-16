"""Retrieve relevant documentation chunks for a query."""

from typing import List, Optional, Tuple

from app.config import config
from app.rag.indexer import rag_indexer


def get_column_clarification(table_name: str, column_name: str = "", profile_id: str = None) -> str:
    """Get admin clarifications for a specific table/column.

    Performs an exact lookup (case-insensitive) in the clarification store.
    This is faster and more precise than semantic search for known columns.

    Args:
        table_name: Table name to look up.
        column_name: Column name (optional — empty for table-level clarifications).
        profile_id: Database profile ID. If None, tries to infer from active connection.

    Returns:
        Formatted clarification text, or empty string if none found.
    """
    try:
        from app.rag.admin_clarifications import clarification_store

        # Auto-detect profile_id from active database connection if not provided
        if profile_id is None:
            try:
                from app.database.connection import db
                if db.current_profile:
                    profile_id = db.current_profile.id
            except Exception:
                pass

        if profile_id is None:
            return ""

        entries = clarification_store.get_clarification(profile_id, table_name, column_name)
        if not entries:
            return ""

        parts = []
        for entry in entries:
            if entry.column_name:
                parts.append(f"- `{entry.table_name}`.`{entry.column_name}`: {entry.answer}")
            else:
                parts.append(f"- `{entry.table_name}`: {entry.answer}")
        return "\n".join(parts)
    except Exception:
        return ""


def retrieve_chunks(query: str, top_k: int = None) -> List[Tuple[str, float]]:
    """
    Search documentation for relevant chunks.

    Args:
        query: User's natural language query.
        top_k: Number of chunks to retrieve (default from config).

    Returns:
        List of (chunk_text, relevance_score) tuples.
    """
    if not rag_indexer.ready:
        return []

    if top_k is None:
        top_k = config.RAG_TOP_K

    return rag_indexer.search(query, top_k)


def retrieve_context(query: str, top_k: int = None) -> str:
    """
    Retrieve and format documentation context for the LLM prompt.

    Returns a formatted string of relevant documentation.
    """
    chunks = retrieve_chunks(query, top_k)
    if not chunks:
        return ""

    parts = ["## 相关文档内容\n"]
    for i, (chunk, score) in enumerate(chunks):
        parts.append(f"### 文档片段 {i+1}（相关度: {score:.2f}）")
        parts.append(chunk)
        parts.append("")

    return "\n".join(parts)
