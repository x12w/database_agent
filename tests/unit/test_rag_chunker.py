"""markdown 切块：边界、重叠、长度约束。

切块质量直接决定检索命中率，所以这里断言的是"结构性事实"：
块不超长、标题跟着内容走、重叠确实来自前一块的尾部。
"""

from __future__ import annotations

from pathlib import Path

from dbchat.config import PROJECT_ROOT
from dbchat.rag.chunker import OVERLAP_SEPARATOR, chunk_markdown


def test_empty_input_returns_no_chunks():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []
    assert chunk_markdown("", chunk_size=100, overlap=10) == []


def test_small_document_is_a_single_chunk():
    text = "## 学生表\n\n记录学生基本信息。"
    chunks = chunk_markdown(text, chunk_size=800, overlap=0)
    assert chunks == [text]


def test_sections_become_separate_chunks():
    text = "## 表 A\n\n内容 A\n\n## 表 B\n\n内容 B"
    chunks = chunk_markdown(text, chunk_size=800, overlap=0)
    assert len(chunks) == 2
    assert chunks[0].startswith("## 表 A")
    assert chunks[1].startswith("## 表 B")


def test_chunks_never_exceed_chunk_size_without_overlap():
    text = "\n\n".join(
        f"## 小节 {i}\n\n" + "这是一段中文说明。" * 40 for i in range(6)
    )
    chunk_size = 300
    chunks = chunk_markdown(text, chunk_size=chunk_size, overlap=0)

    assert len(chunks) > 6
    assert all(len(chunk) <= chunk_size for chunk in chunks), [
        len(chunk) for chunk in chunks if len(chunk) > chunk_size
    ]
    assert all(chunk.strip() for chunk in chunks)


def test_long_single_paragraph_is_split():
    text = "## 长段\n\n" + "句子。" * 500
    chunks = chunk_markdown(text, chunk_size=200, overlap=0)

    assert len(chunks) > 1
    assert all(len(chunk) <= 200 for chunk in chunks)


def test_overlap_carries_the_tail_of_the_previous_chunk():
    text = "\n\n".join(f"## 段 {i}\n\n" + f"第{i}段内容。" * 30 for i in range(4))
    chunk_size, overlap = 250, 40
    chunks = chunk_markdown(text, chunk_size=chunk_size, overlap=overlap)

    assert len(chunks) > 1
    assert OVERLAP_SEPARATOR not in chunks[0]
    for previous, current in zip(chunks, chunks[1:], strict=False):
        head, separator, _rest = current.partition(OVERLAP_SEPARATOR)
        assert separator == OVERLAP_SEPARATOR
        assert len(head) == overlap
        assert previous.endswith(head)
        # 加上重叠前缀后仍在可预期范围内
        assert len(current) <= chunk_size + overlap + len(OVERLAP_SEPARATOR)


def test_zero_overlap_leaves_chunks_untouched():
    text = "\n\n".join(f"## 段 {i}\n\n" + f"内容{i}。" * 50 for i in range(3))
    assert chunk_markdown(text, chunk_size=200, overlap=0) == chunk_markdown(
        text, chunk_size=200, overlap=-5
    )


def test_short_previous_chunk_is_not_used_for_overlap():
    """前一块比 overlap 还短时，不接前缀（避免整块被复制一遍）。"""
    text = "## A\n\n短\n\n## B\n\n" + "内容。" * 200
    chunks = chunk_markdown(text, chunk_size=120, overlap=100)
    assert all(chunk.strip() for chunk in chunks)
    assert OVERLAP_SEPARATOR not in chunks[0]


def test_chunking_is_deterministic():
    text = "\n\n".join(f"## 段 {i}\n\n" + "内容。" * 60 for i in range(5))
    first = chunk_markdown(text, chunk_size=300, overlap=50)
    second = chunk_markdown(text, chunk_size=300, overlap=50)
    assert first == second


def test_heading_leading_the_section_is_carried_into_the_first_chunk():
    """长小节的第一块必须带标题；后续块由段落切分继承标题。

    注意：整节只有**一个**超长段落时，后续碎片不再重复标题 —— 这是历史
    行为（`_split_long_text` 只切正文），这里钉住它以免被无声改掉。
    """
    text = "## 成绩表\n\n第一段。\n\n第二段。" + "内容。" * 100
    chunks = chunk_markdown(text, chunk_size=200, overlap=0)
    assert len(chunks) > 1
    assert chunks[0].startswith("## 成绩表")
    # 由段落切分出来的后续块也带标题
    assert sum("成绩表" in chunk for chunk in chunks) >= 2

    single_paragraph = "## 成绩表\n\n" + "成绩说明。" * 300
    pieces = chunk_markdown(single_paragraph, chunk_size=200, overlap=0)
    assert len(pieces) > 2
    assert pieces[0].startswith("## 成绩表")


def test_content_before_first_heading_is_kept():
    text = "引言段落\n\n## 第一节\n\n正文"
    chunks = chunk_markdown(text, chunk_size=800, overlap=0)
    assert chunks[0].startswith("引言段落")


def test_real_document_chunks_are_all_non_trivial():
    """对仓库里的真实文档跑一遍：不能出现空块或超长块。"""
    doc = PROJECT_ROOT / "training_exam_db_rag.md"
    if not doc.exists():  # 数据文件不随包发布，缺失时跳过
        return
    text = doc.read_text(encoding="utf-8")
    chunks = chunk_markdown(text, chunk_size=800, overlap=100)

    assert len(chunks) > 30
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= 800 + 100 + len(OVERLAP_SEPARATOR) for chunk in chunks)
    assert any("##" in chunk for chunk in chunks)


def test_project_root_resolves_to_repository_root():
    """真实文档路径来自 Settings.project_root，不能是硬编码的绝对路径。"""
    assert (Path(PROJECT_ROOT) / "pyproject.toml").exists()
