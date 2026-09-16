"""字面量感知扫描器 —— 针对历史 bug 的回归测试。

`app/database/connection.py:73-79` 的 `sql.replace("%s", "?")` 会把字符串
字面量、注释里的 `%s` 一起改写。下面每一条都对应它改坏过的一种写法。
"""

from __future__ import annotations

import pytest

from dbchat.db.sqltext import (
    SegmentKind,
    first_keyword,
    iter_segments,
    mask_literals,
    rewrite_placeholders,
    split_qualified_name,
    split_statements,
    strip_comments,
)


# ---------------------------------------------------------- 占位符改写


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # 普通占位符：改
        ("SELECT * FROM t WHERE a = %s", "SELECT * FROM t WHERE a = ?"),
        ("SELECT * FROM t WHERE a = %s AND b = %s", "SELECT * FROM t WHERE a = ? AND b = ?"),
        # 字符串字面量里的 %s：不改（旧实现改成 '?%'，查询结果直接错）
        ("SELECT * FROM t WHERE note LIKE '%s%'", "SELECT * FROM t WHERE note LIKE '%s%'"),
        ("SELECT '100%s' FROM DUAL", "SELECT '100%s' FROM DUAL"),
        ("SELECT * FROM t WHERE a = %s AND b LIKE '%%s%%'", "SELECT * FROM t WHERE a = ? AND b LIKE '%%s%%'"),
        # 注释里的 %s：不改
        ("-- 占位符是 %s\nSELECT %s FROM DUAL", "-- 占位符是 %s\nSELECT ? FROM DUAL"),
        ("SELECT %s /* 这里写 %s */ FROM DUAL", "SELECT ? /* 这里写 %s */ FROM DUAL"),
        ("SELECT %s # 这里也写 %s\nFROM DUAL", "SELECT ? # 这里也写 %s\nFROM DUAL"),
        # 双引号标识符里的 %s：不改
        ('SELECT "col%s" FROM t WHERE a = %s', 'SELECT "col%s" FROM t WHERE a = ?'),
        ("SELECT `col%s` FROM t WHERE a = %s", "SELECT `col%s` FROM t WHERE a = ?"),
        # %% 是转义后的百分号，整体保留（`%%s` 里的 %s 不是占位符）
        ("SELECT 'a' WHERE x LIKE '%%' AND y = %s", "SELECT 'a' WHERE x LIKE '%%' AND y = ?"),
        ("SELECT '%%s' FROM t", "SELECT '%%s' FROM t"),
        ("SELECT a FROM t WHERE p = %s AND q LIKE 'x%%'", "SELECT a FROM t WHERE p = ? AND q LIKE 'x%%'"),
        # 重复引号转义
        ("SELECT 'it''s %s' FROM t WHERE a = %s", "SELECT 'it''s %s' FROM t WHERE a = ?"),
        # 反斜杠转义
        (r"SELECT 'a\'s %s' FROM t WHERE a = %s", r"SELECT 'a\'s %s' FROM t WHERE a = ?"),
        # 美元引用（PG）
        ("SELECT $$%s$$ FROM t WHERE a = %s", "SELECT $$%s$$ FROM t WHERE a = ?"),
        ("SELECT $tag$%s$tag$ FROM t WHERE a = %s", "SELECT $tag$%s$tag$ FROM t WHERE a = ?"),
        # PG 的 #> 运算符不能被当成注释
        ("SELECT a #> '{b}' FROM t WHERE c = %s", "SELECT a #> '{b}' FROM t WHERE c = ?"),
    ],
)
def test_rewrite_placeholders_is_literal_aware(sql: str, expected: str) -> None:
    assert rewrite_placeholders(sql, "%s", "?") == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE a = %s",
        "SELECT * FROM t WHERE note LIKE '%s%'",
        "-- %s\nSELECT 1",
    ],
)
def test_rewrite_placeholders_identity_is_noop(sql: str) -> None:
    """src == dst 时原样返回（MySQL / PG 走的就是这条恒等路径）。"""
    assert rewrite_placeholders(sql, "%s", "%s") == sql


def test_rewrite_placeholders_handles_unterminated_literal() -> None:
    """未闭合的引号不能把扫描器带崩。"""
    sql = "SELECT 'unterminated %s"
    assert rewrite_placeholders(sql, "%s", "?") == sql


# ---------------------------------------------------------- 注释剥离


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1 -- 注释\nFROM t", "SELECT 1  \nFROM t"),
        ("SELECT 1 /* 注释 */ FROM t", "SELECT 1   FROM t"),
        ("SELECT 1 # 注释\nFROM t", "SELECT 1  \nFROM t"),
        # 注释剥离后不能把相邻 token 粘在一起
        ("SELECT 1/*x*/FROM t", "SELECT 1 FROM t"),
        # 字符串里的注释符号不是注释
        ("SELECT '-- not a comment' FROM t", "SELECT '-- not a comment' FROM t"),
        ("SELECT '/* not a comment */' FROM t", "SELECT '/* not a comment */' FROM t"),
        ("SELECT '#' FROM t", "SELECT '#' FROM t"),
        # PG 的 #> 不是注释
        ("SELECT a #> '{b}' FROM t", "SELECT a #> '{b}' FROM t"),
        ("SELECT a #>> '{b}' FROM t", "SELECT a #>> '{b}' FROM t"),
    ],
)
def test_strip_comments(sql: str, expected: str) -> None:
    assert strip_comments(sql) == expected


def test_strip_comments_keeps_line_structure() -> None:
    """单行注释只吃掉本行，换行要保留（否则错误定位会串行）。"""
    sql = "SELECT 1 -- c\nSELECT 2 -- d\nSELECT 3"
    assert strip_comments(sql).count("\n") == sql.count("\n")


# ---------------------------------------------------------- 语句切分


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", ["SELECT 1"]),
        ("SELECT 1;", ["SELECT 1"]),
        ("SELECT 1; SELECT 2", ["SELECT 1", "SELECT 2"]),
        ("SELECT 1;SELECT 2;", ["SELECT 1", "SELECT 2"]),
        # 字面量里的分号不是分隔符
        ("SELECT ';' FROM t", ["SELECT ';' FROM t"]),
        ("SELECT ';' FROM t; SELECT 2", ["SELECT ';' FROM t", "SELECT 2"]),
        ('SELECT ";" FROM t; SELECT 2', ['SELECT ";" FROM t', "SELECT 2"]),
        # 注释里的分号同样不是
        ("SELECT 1 -- ;\nFROM t", ["SELECT 1 -- ;\nFROM t"]),
        ("SELECT 1 /* ; */ FROM t", ["SELECT 1 /* ; */ FROM t"]),
        # 纯注释片段被丢弃
        ("-- 只是注释", []),
        ("SELECT 1; -- 只是注释", ["SELECT 1"]),
        ("", []),
        ("   ;  ; ", []),
    ],
)
def test_split_statements(sql: str, expected: list[str]) -> None:
    assert split_statements(sql) == expected


def test_split_statements_multi_is_detected() -> None:
    """多语句检测是安全控制：字符串里的分号不能触发误判。"""
    assert len(split_statements("SELECT 1; DROP TABLE x")) == 2
    assert len(split_statements("SELECT 'a;b' FROM t")) == 1


# ---------------------------------------------------------- 其它助手


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT 1", "SELECT"),
        ("  select 1", "SELECT"),
        ("/*c*/ SELECT 1", "SELECT"),
        ("-- c\nWITH t AS (SELECT 1) SELECT * FROM t", "WITH"),
        ("", ""),
        ("'SELECT'", ""),
    ],
)
def test_first_keyword(sql: str, expected: str) -> None:
    assert first_keyword(sql) == expected


def test_mask_literals_hides_data_only() -> None:
    """`WHERE name = 'DELETE'` 里的 DELETE 是数据，不是写操作。"""
    masked = mask_literals("SELECT * FROM t WHERE name = 'DELETE'")
    assert "DELETE" not in masked
    assert "SELECT * FROM t WHERE name = " in masked

    # 代码里的 DELETE 仍在
    assert "DELETE" in mask_literals("DELETE FROM t WHERE name = 'x'")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("employee", (None, "employee")),
        ("db.employee", ("db", "employee")),
        ('"db"."employee"', ("db", "employee")),
        ("`db`.`employee`", ("db", "employee")),
        ("`employee`", (None, "employee")),
        ('"we""ird"', (None, 'we"ird')),
        ("catalog.db.employee", ("db", "employee")),
        ("", (None, "")),
    ],
)
def test_split_qualified_name(raw: str, expected: tuple[str | None, str]) -> None:
    assert split_qualified_name(raw) == expected


def test_iter_segments_classifies() -> None:
    """分段器本身也被测到：种类判定是上面所有助手的基础。"""
    segments = list(iter_segments("SELECT 'a' FROM \"t\" -- c\n/* b */ WHERE x = $$y$$"))
    kinds = [seg.kind for seg in segments]
    assert SegmentKind.STRING in kinds
    assert SegmentKind.IDENTIFIER in kinds
    assert SegmentKind.LINE_COMMENT in kinds
    assert SegmentKind.BLOCK_COMMENT in kinds
    assert SegmentKind.DOLLAR_QUOTE in kinds

    # 每段都能对回原文
    sql = "SELECT 'a' FROM \"t\" -- c\n/* b */ WHERE x = $$y$$"
    for segment in segments:
        assert sql[segment.start : segment.end] == segment.text
    assert "".join(seg.text for seg in segments) == sql
