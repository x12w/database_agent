"""SQL 文本扫描 —— 字面量感知。

历史实现（`app/database/connection.py:73-79`）在每次执行前做
`sql.replace("%s", "?")`，把字符串字面量、注释里的 `%s` 一并改写::

    WHERE note LIKE '%s%'   ->   WHERE note LIKE '?%'
    SELECT '100%s'          ->   SELECT '100?'
    -- 占位符 %s 说明          ->   -- 占位符 ? 说明

同一个函数还有第二重缺陷：它先判断 `if "%s" in sql` 再改写，然后
`if params: execute(sql, params) else: execute(sql)` —— 于是"含占位符但
没给参数"的调用会把 `?` 发给驱动而一个绑定值都不给，报出的是驱动的
绑定错误而不是调用方的参数错误。

本模块用**单遍状态机**取代字符串替换：只在"代码态"里改写，进入
`'...'` / `"..."` / `` `...` `` / `--` / `#` / `/* */` / `$tag$...$tag$`
之后原样复制。

关于转义的已知取舍（必须在调用方知情）：

* 单引号串同时接受 `''` 与 `\\'` 两种转义。前者是 SQL 标准与 PG
  （`standard_conforming_strings=on`）的语义，后者是 MySQL 的语义，
  两种写法的 SQL 在本扫描器里都能正确切分；但 `'a\\'` 这种"反斜杠
  在标准语义下是字面量"的写法会被当作未闭合串，从而把后续文本一并
  视为字符串。这是无方言信息时的必然折中。
* 块注释不支持嵌套（MySQL 不嵌套；PG 嵌套，但嵌套注释在生成的 SQL 中
  不出现）。
* `#` 仅在前一个字符是行首或空白、且后一个字符不是 `>` 时视为注释 ——
  这样既能吃掉 MySQL 的 `# 注释`，又不会误伤 PG 的 `#>` / `#>>` 运算符。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Segment",
    "SegmentKind",
    "first_keyword",
    "iter_segments",
    "mask_literals",
    "rewrite_placeholders",
    "split_qualified_name",
    "split_statements",
    "strip_comments",
]


class SegmentKind(StrEnum):
    """一段 SQL 文本的形态。"""

    CODE = "code"                    # 代码（可改写）
    STRING = "string"                # '...'
    IDENTIFIER = "identifier"        # "..." 或 `...`
    LINE_COMMENT = "line_comment"    # -- ... 或 # ...
    BLOCK_COMMENT = "block_comment"  # /* ... */
    DOLLAR_QUOTE = "dollar_quote"    # $$...$$ / $tag$...$tag$


#: 字面量形态（不可改写的内容）。
_LITERAL_KINDS = frozenset(
    {
        SegmentKind.STRING,
        SegmentKind.IDENTIFIER,
        SegmentKind.DOLLAR_QUOTE,
    }
)
_COMMENT_KINDS = frozenset({SegmentKind.LINE_COMMENT, SegmentKind.BLOCK_COMMENT})


@dataclass(slots=True, frozen=True)
class Segment:
    """SQL 中的一段连续文本。

    `start` 是原文中的起始下标，`text` 与 `sql[start:start+len(text)]` 相等。
    """

    kind: SegmentKind
    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)

    @property
    def is_code(self) -> bool:
        return self.kind is SegmentKind.CODE

    @property
    def is_literal(self) -> bool:
        return self.kind in _LITERAL_KINDS

    @property
    def is_comment(self) -> bool:
        return self.kind in _COMMENT_KINDS


def _scan_quoted(sql: str, i: int, quote: str) -> int:
    """从起始引号 `i` 扫到闭合引号之后；未闭合则返回 `len(sql)`。"""
    n = len(sql)
    j = i + 1
    while j < n:
        ch = sql[j]
        if ch == "\\" and quote != "`":
            # 反斜杠转义：跳过下一个字符。反引号标识符不接受反斜杠转义。
            j += 2
            continue
        if ch == quote:
            if j + 1 < n and sql[j + 1] == quote:
                j += 2  # 重复引号 = 转义后的引号本身
                continue
            return j + 1
        j += 1
    return n


def _scan_block_comment(sql: str, i: int) -> int:
    """从 `/*` 扫到 `*/` 之后；未闭合则返回 `len(sql)`。"""
    n = len(sql)
    j = sql.find("*/", i + 2)
    return n if j < 0 else j + 2


def _scan_line_comment(sql: str, i: int) -> int:
    """从注释起始扫到行尾（不含换行符）。"""
    n = len(sql)
    j = sql.find("\n", i)
    return n if j < 0 else j


def _dollar_tag_at(sql: str, i: int) -> str | None:
    """若 `i` 处是一个美元引用定界符（`$$` 或 `$tag$`），返回它。

    只在本行之后确实存在成对的定界符时才认定，避免把 `$1` 之类误判。
    """
    n = len(sql)
    j = i + 1
    while j < n and (sql[j].isalnum() or sql[j] == "_"):
        j += 1
    if j >= n or sql[j] != "$":
        return None
    tag = sql[i : j + 1]
    return tag if sql.find(tag, j + 1) >= 0 else None


def _is_hash_comment(sql: str, i: int) -> bool:
    """`#` 是否为注释起始（而非 PG 的 `#>` / `#>>` 运算符）。"""
    if i + 1 < len(sql) and sql[i + 1] == ">":
        return False
    return i == 0 or sql[i - 1] in " \t\r\n(,;"


def iter_segments(sql: str) -> Iterator[Segment]:
    """把 SQL 切成"代码段 / 字面量段 / 注释段"。

    单遍扫描，不做正则回溯；相邻同态字符合并为一段。
    """
    n = len(sql)
    i = 0
    start = 0
    while i < n:
        ch = sql[i]
        kind: SegmentKind | None = None
        nxt = i

        if ch == "'":
            kind, nxt = SegmentKind.STRING, _scan_quoted(sql, i, "'")
        elif ch == '"':
            kind, nxt = SegmentKind.IDENTIFIER, _scan_quoted(sql, i, '"')
        elif ch == "`":
            kind, nxt = SegmentKind.IDENTIFIER, _scan_quoted(sql, i, "`")
        elif ch == "-" and sql.startswith("--", i) or ch == "#" and _is_hash_comment(sql, i):
            kind, nxt = SegmentKind.LINE_COMMENT, _scan_line_comment(sql, i)
        elif ch == "/" and sql.startswith("/*", i):
            kind, nxt = SegmentKind.BLOCK_COMMENT, _scan_block_comment(sql, i)
        elif ch == "$":
            tag = _dollar_tag_at(sql, i)
            if tag is not None:
                close = sql.find(tag, i + len(tag))
                kind, nxt = SegmentKind.DOLLAR_QUOTE, close + len(tag)

        if kind is None:
            i += 1
            continue

        if i > start:
            yield Segment(SegmentKind.CODE, sql[start:i], start)
        yield Segment(kind, sql[i:nxt], i)
        start = i = nxt

    if start < n:
        yield Segment(SegmentKind.CODE, sql[start:n], start)


def _replace_ignoring_percent_escape(text: str, src: str, dst: str) -> str:
    """在代码段内替换占位符，`%%` 作为整体跳过。

    `%%s` 中的 `%s` 不是占位符（`%%` 是转义后的百分号），必须原样保留。
    """
    if src not in text:
        return text
    out: list[str] = []
    i = 0
    n = len(text)
    step = len(src)
    while i < n:
        if text.startswith("%%", i):
            out.append("%%")
            i += 2
            continue
        if text.startswith(src, i):
            out.append(dst)
            i += step
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def rewrite_placeholders(sql: str, src: str, dst: str) -> str:
    """把占位符 `src` 改写为 `dst`，**跳过字面量与注释**。

    单遍、状态跟踪；`%%` 视作整体保留。`src == dst` 时原样返回。
    """
    if not sql or not src or src == dst:
        return sql
    return "".join(
        _replace_ignoring_percent_escape(seg.text, src, dst) if seg.is_code else seg.text
        for seg in iter_segments(sql)
    )


def strip_comments(sql: str) -> str:
    """去掉注释。

    注释被替换成**一个空格**而不是直接删除 —— 直接删除会把
    `SELECT 1/*x*/FROM t` 粘成 `SELECT 1FROM t`。
    """
    return "".join(" " if seg.is_comment else seg.text for seg in iter_segments(sql))


def mask_literals(sql: str, mask: str = " ") -> str:
    """把字符串字面量替换成 `mask`，保留代码与注释。

    用于"扫描危险关键字"这类场景：`WHERE name = 'DELETE'` 里的
    `DELETE` 只是数据，不该被当成写操作；`DELETE FROM t` 仍在代码里，
    依然会被命中。
    """
    return "".join(mask if seg.kind is SegmentKind.STRING else seg.text for seg in iter_segments(sql))


def split_statements(sql: str) -> list[str]:
    """按**字面量之外**的分号切分语句。

    只含空白或注释的片段会被丢弃。返回值保留原文（含注释与大小写）。
    """
    if not sql:
        return []
    parts: list[str] = []
    start = 0
    for seg in iter_segments(sql):
        if not seg.is_code:
            continue
        for offset, ch in enumerate(seg.text):
            if ch == ";":
                parts.append(sql[start : seg.start + offset])
                start = seg.start + offset + 1
    parts.append(sql[start:])
    return [p.strip() for p in parts if strip_comments(p).strip()]


def first_keyword(sql: str) -> str:
    """取首个关键字（跳过注释与空白），转大写。无内容时返回空串。"""
    cleaned = strip_comments(sql).lstrip()
    if not cleaned:
        return ""
    end = 0
    while end < len(cleaned) and (cleaned[end].isalnum() or cleaned[end] == "_"):
        end += 1
    return cleaned[:end].upper()


def split_qualified_name(name: str) -> tuple[str | None, str]:
    """把 `"sch"."tbl"` / `` `tbl` `` / `sch.tbl` 拆成 (schema, table)。

    会去掉标识符引用符。多于两段时只保留最后两段（catalog 被忽略）。
    """
    parts = _split_on_dots(name)
    if not parts:
        return None, ""
    if len(parts) == 1:
        return None, parts[0]
    return parts[-2], parts[-1]


def _unquote_identifier(raw: str) -> str:
    """去掉一段标识符两端的引用符。"""
    if len(raw) >= 2 and raw[0] in ('"', "`", "[") and raw[-1] in ('"', "`", "]"):
        inner = raw[1:-1]
        return inner.replace(raw[0] * 2, raw[0]) if raw[0] != "[" else inner
    return raw


def _split_on_dots(name: str) -> list[str]:
    """按点拆分标识符；出现在引号内的点不算分隔符。"""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(name)
    while i < n:
        ch = name[i]
        if ch in ('"', "`", "["):
            if ch == "[":
                close = name.find("]", i + 1)
                j = n if close < 0 else close + 1
            else:
                j = _scan_quoted(name, i, ch)
            buf.append(_unquote_identifier(name[i:j]))
            i = j
            continue
        if ch == ".":
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return [p for p in parts if p]
