"""把 markdown 文档切成可检索的片段。

切分策略（从历史实现原样移植，检索质量依赖它，不要随意调整）：

1. 先按 `## ` 二级标题切段；
2. 段落不超过 `chunk_size` 就整段成块（保留标题上下文）；
3. 超长段落按空行切，再按句子切，最后按字符兜底；
4. 相邻块之间从前一块尾部借 `overlap` 个字符做重叠，用 `\\n...\\n` 分隔。
"""

from __future__ import annotations

import re

#: 重叠拼接时插入的分隔标记。
OVERLAP_SEPARATOR = "\n...\n"

#: 句末标点（中英文都算）。
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？.!?])\s*")


def chunk_markdown(text: str, chunk_size: int = 800, overlap: int = 100) -> list[str]:
    """把一份 markdown 切成带重叠的片段。

    Args:
        text: 文档全文。
        chunk_size: 单块目标长度（字符数，不是 token）。
        overlap: 相邻块的重叠字符数；<= 0 表示不重叠。

    Returns:
        片段列表；空文档返回空列表。每个片段都已 strip。
    """
    if not text or not text.strip():
        return []

    sections = _split_by_sections(text)

    chunks: list[str] = []
    for section_title, section_text in sections:
        if len(section_text) <= chunk_size:
            chunks.append(section_text)
            continue

        paragraphs = _split_paragraphs(section_text)
        current_chunk = section_title + "\n\n" if section_title else ""

        for para in paragraphs:
            if len(current_chunk) + len(para) + 2 <= chunk_size:
                if current_chunk and not current_chunk.endswith("\n\n"):
                    current_chunk += "\n\n"
                current_chunk += para
            else:
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                current_chunk = section_title + "\n\n" + para if section_title else para

                # 单段本身就超长，按句子/字符再切
                if len(current_chunk) > chunk_size:
                    chunks.extend(_split_long_text(current_chunk, chunk_size, overlap))
                    current_chunk = ""

        if current_chunk.strip():
            chunks.append(current_chunk.strip())

    return _with_overlap(chunks, overlap)


def _with_overlap(chunks: list[str], overlap: int) -> list[str]:
    """给每个非首块接上前一块的尾部。"""
    if overlap <= 0 or len(chunks) <= 1:
        return chunks
    overlapped = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = chunks[i - 1]
        curr = chunks[i]
        if len(prev) > overlap:
            curr = prev[-overlap:] + OVERLAP_SEPARATOR + curr
        overlapped.append(curr)
    return overlapped


def _split_by_sections(text: str) -> list[tuple[str, str]]:
    """按 `## ` 标题切成 (标题, 正文) 对。文首未命中标题的内容归入空标题段。"""
    sections: list[tuple[str, str]] = []
    current_title = ""
    current_lines: list[str] = []

    for line in text.split("\n"):
        if line.startswith("## "):
            if current_lines:
                sections.append((current_title, "\n".join(current_lines)))
            current_title = line
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_title, "\n".join(current_lines)))

    return sections


def _split_paragraphs(text: str) -> list[str]:
    """按空行切段落。"""
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _split_long_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """超长文本按句子切；单句仍超长时按字符硬切。"""
    chunks: list[str] = []
    current = ""

    for sentence in _SENTENCE_SPLIT.split(text):
        if len(current) + len(sentence) <= chunk_size:
            current += sentence
            continue

        if current.strip():
            chunks.append(current.strip())

        if len(sentence) > chunk_size:
            step = max(1, chunk_size - overlap)
            for i in range(0, len(sentence), step):
                chunks.append(sentence[i : i + chunk_size])
            current = ""
        else:
            current = sentence

    if current.strip():
        chunks.append(current.strip())

    return chunks


__all__ = ["OVERLAP_SEPARATOR", "chunk_markdown"]
