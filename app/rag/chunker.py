"""Split markdown document into searchable chunks."""

import re
from typing import List


def chunk_markdown(text: str, chunk_size: int = 800, overlap: int = 100) -> List[str]:
    """
    Split a markdown document into overlapping chunks by section headings.

    Each chunk preserves context by including the section heading and
    maintaining paragraph boundaries where possible.
    """
    if not text or not text.strip():
        return []

    # Split by ## headings (sections)
    sections = _split_by_sections(text)

    chunks = []
    for section_title, section_text in sections:
        if len(section_text) <= chunk_size:
            # Small section, keep as one chunk
            chunks.append(section_text)
        else:
            # Large section, split by paragraphs
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

                    # If single paragraph exceeds chunk size, split it
                    if len(current_chunk) > chunk_size:
                        sub_chunks = _split_long_text(current_chunk, chunk_size, overlap)
                        chunks.extend(sub_chunks)
                        current_chunk = ""

            if current_chunk.strip():
                chunks.append(current_chunk.strip())

    # Add overlap by appending overlapping tail from previous chunk
    if overlap > 0 and len(chunks) > 1:
        overlapped = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = chunks[i - 1]
            curr = chunks[i]
            if len(prev) > overlap:
                overlap_text = prev[-overlap:]
                curr = overlap_text + "\n...\n" + curr
            overlapped.append(curr)
        chunks = overlapped

    return chunks


def _split_by_sections(text: str) -> List[tuple]:
    """Split text into (section_title, section_content) pairs."""
    # Remove document title line if present
    lines = text.split("\n")

    sections = []
    current_title = ""
    current_lines = []

    for line in lines:
        if line.startswith("## "):
            # Save previous section
            if current_lines:
                sections.append((current_title, "\n".join(current_lines)))
            current_title = line
            current_lines = [line]
        else:
            current_lines.append(line)

    # Don't forget the last section
    if current_lines:
        sections.append((current_title, "\n".join(current_lines)))

    return sections


def _split_paragraphs(text: str) -> List[str]:
    """Split text into paragraphs (separated by blank lines)."""
    raw = text.split("\n\n")
    return [p.strip() for p in raw if p.strip()]


def _split_long_text(text: str, chunk_size: int, overlap: int) -> List[str]:
    """Split a text that's too long into sentence-aware chunks."""
    sentences = re.split(r'(?<=[。！？.!?])\s*', text)
    chunks = []
    current = ""

    for sent in sentences:
        if len(current) + len(sent) <= chunk_size:
            current += sent
        else:
            if current.strip():
                chunks.append(current.strip())
            # If a single sentence is too long, split by characters
            if len(sent) > chunk_size:
                for i in range(0, len(sent), chunk_size - overlap):
                    chunks.append(sent[i:i + chunk_size])
                current = ""
            else:
                current = sent

    if current.strip():
        chunks.append(current.strip())

    return chunks
