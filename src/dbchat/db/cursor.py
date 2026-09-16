"""统一的游标与行映射。

历史实现有三份互不相同、各自漏事的行包装（`app/database/connection.py:17-122`）：

* `_PGCursorWrapper` —— 只为 PG 做大小写归一，其余驱动拿到的是原样行；
* `_TupleCursorWrapper` —— 只为达梦做"元组转字典"，顺带做占位符替换；
* `_CaseInsensitiveDict` —— 只把键转大写，`items()` / 迭代拿到的仍是
  大写键，谁都得记住"内部键是大写的"。

这里收敛成一条路径：`DialectCursor` 只负责"取数据 + 交给方言归一"，
行形状差异全部由 `Dialect.normalize_row` 承担（PG 的小写列名、达梦的
元组行、MySQL 的 DictCursor）。

行映射保留驱动给出的原始键大小写，另建一份小写索引 —— 于是
`row["TABLE_NAME"]`、`row["table_name"]`、`row.get("Table_Name")` 都成立，
而 `row.keys()` 给出的仍是数据库真实的列名（序列化成 JSON 时不会莫名
变成全大写）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from dbchat.db.sqltext import rewrite_placeholders

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，避免运行时循环导入
    from dbchat.db.dialects.base import Dialect

__all__ = ["CaseInsensitiveRow", "DialectCursor", "coerce_jsonable", "mapping_from_row"]

logger = logging.getLogger(__name__)

#: 探测"占位符是否出现在代码态"用的哨兵字符（SQL 里不可能自然出现）。
_UNBOUND_SENTINEL = "\x00"


def coerce_jsonable(value: Any) -> Any:
    """把驱动特定类型转成可 JSON 化的值。

    Decimal / 日期时间 / 二进制三种在 MySQL、PG、达梦上都会遇到，
    三个方言共用这一份实现（差异部分由各自 `coerce_value` 追加）。

    转换后仍与 `dbchat.domain.query._family_of` 的判定一致：
    数值仍是 float、时间仍是 ISO 串、二进制仍是可读文本。
    """
    import datetime
    import decimal

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _coerce_bytes(bytes(value))
    if isinstance(value, (set, frozenset)):
        # MySQL 的 SET 类型
        return ",".join(sorted(str(v) for v in value))
    return value


def _coerce_bytes(raw: bytes) -> str:
    """二进制列：能按 UTF-8 解出文本就还原文本，否则给十六进制。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return "0x" + raw.hex()


class CaseInsensitiveRow(dict):
    """大小写不敏感的字典行。

    键保留原始大小写，查找走一份小写索引。列名只差大小写的两列
    （PG 允许 `"a"` 与 `"A"` 并存）会共用索引，后者覆盖前者。
    """

    __slots__ = ("_index",)

    def __init__(self, mapping: Mapping[str, Any] | None = None) -> None:
        super().__init__(mapping or {})
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._index = {str(k).lower(): k for k in self.keys()}

    # ---- 查询 ----
    def __getitem__(self, key: str) -> Any:
        if super().__contains__(key):
            return super().__getitem__(key)
        actual = self._index.get(str(key).lower())
        if actual is None:
            raise KeyError(key) from None
        return super().__getitem__(actual)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        if super().__contains__(key):
            return True
        return isinstance(key, str) and key.lower() in self._index

    def keys_lower(self) -> set[str]:
        """全部键的小写形式。"""
        return set(self._index)

    # ---- 修改（保持索引同步）----
    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self._index[str(key).lower()] = key

    def __delitem__(self, key: str) -> None:
        try:
            actual = key if super().__contains__(key) else self._index.get(str(key).lower())
        except TypeError:  # pragma: no cover - 非字符串键
            actual = key
        if actual is None:
            raise KeyError(key)
        super().__delitem__(actual)
        self._rebuild_index()

    def pop(self, key: str, *args: Any) -> Any:
        actual = key if super().__contains__(key) else self._index.get(str(key).lower())
        if actual is None:
            if args:
                return args[0]
            raise KeyError(key)
        value = super().pop(actual)
        self._rebuild_index()
        return value

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        super().update(*args, **kwargs)
        self._rebuild_index()

    def copy(self) -> CaseInsensitiveRow:
        return CaseInsensitiveRow(self)

    def __iter__(self) -> Iterator[str]:
        return super().__iter__()


def mapping_from_row(
    row: Any,
    description: Sequence[Any] | None,
    column_names: Sequence[str] | None = None,
) -> CaseInsensitiveRow | None:
    """把驱动返回的一行变成 `CaseInsensitiveRow`。

    两种行形状都要吃下：映射行（MySQL DictCursor、PG RealDictCursor）
    直接复用；元组行（达梦）按 `description` 的列序位置映射。

    Args:
        row: 驱动返回的单行。
        description: `cursor.description`。
        column_names: 预先算好的列名，避免每行都重算。

    Returns:
        归一后的行；`row` 为 None 时返回 None。
    """
    if row is None:
        return None
    if isinstance(row, Mapping):
        return CaseInsensitiveRow(dict(row))

    if column_names is None:
        column_names = [str(item[0]) for item in (description or ())]

    if not column_names:
        # 没有列名就无法给出可用的行。绝不静默 —— 记 warning 并退回下标键，
        # 让调用方能在日志里看到是哪条 SQL 触发的。
        logger.warning("驱动返回了无 description 的元组行，退回下标键: %r", row)
        return CaseInsensitiveRow({str(i): v for i, v in enumerate(row)})

    return CaseInsensitiveRow(
        {name: row[i] for i, name in enumerate(column_names) if i < len(row)}
    )


class DialectCursor:
    """把原始驱动游标包装成"字典行 + 方言占位符"的统一游标。

    替代历史上三份各管一段的包装类。行的归一交给 `Dialect.normalize_row`，
    占位符改写交给 `Dialect.bind`（两者都在方言里，这里不做任何 `if`）。
    """

    __slots__ = ("_raw", "_dialect")

    def __init__(self, raw: Any, dialect: Dialect) -> None:
        self._raw = raw
        self._dialect = dialect

    # ---- 执行 ----

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        """执行 SQL。

        占位符按方言改写（字面量感知），**有参数才绑定**。
        历史实现在这里改写了占位符却在无参数时直接 `execute(sql)`，
        把 `?` 发给驱动而一个绑定值都不给；这里对"有占位符却无参数"
        显式告警。
        """
        bound = self._dialect.bind(sql, len(params) if params else 0)
        if params:
            return self._raw.execute(bound, tuple(params))
        self._warn_if_unbound(bound)
        return self._raw.execute(bound)

    def _warn_if_unbound(self, sql: str) -> None:
        """SQL 里还留着**代码态**的占位符却没有绑定参数时告警。

        判据是"改写哨兵后是否出现过哨兵" —— 而不是"改写后是否还含占位符"：
        `WHERE note LIKE '%s%'` 里的 `%s` 本来就在字面量里，改写后依然存在，
        用后者判断会对每一条带 `%s` 字面量的 SQL 都误报。
        """
        marker = self._dialect.placeholder()
        if not marker or marker not in sql:
            return
        if _UNBOUND_SENTINEL in rewrite_placeholders(sql, marker, _UNBOUND_SENTINEL):
            logger.warning("SQL 含占位符 %r 但没有绑定参数，驱动会报绑定错误: %s", marker, sql[:200])

    # ---- 取数 ----

    @property
    def description(self) -> Sequence[Any] | None:
        return self._raw.description

    def column_names(self) -> list[str]:
        """按方言规则取出列名。"""
        return self._dialect.column_names(self._raw.description)

    def _normalize(self, row: Any) -> CaseInsensitiveRow | None:
        normalized = self._dialect.normalize_row(row, self._raw.description)
        if normalized is None:
            return None
        coerced = {k: self._dialect.coerce_value(v) for k, v in normalized.items()}
        return CaseInsensitiveRow(coerced)

    def fetchall(self) -> list[CaseInsensitiveRow]:
        rows = self._raw.fetchall()
        return [r for r in (self._normalize(row) for row in rows) if r is not None]

    def fetchone(self) -> CaseInsensitiveRow | None:
        return self._normalize(self._raw.fetchone())

    def fetchmany(self, size: int | None = None) -> list[CaseInsensitiveRow]:
        rows = self._raw.fetchmany(size) if size is not None else self._raw.fetchmany()
        return [r for r in (self._normalize(row) for row in rows) if r is not None]

    @property
    def rowcount(self) -> int:
        return int(getattr(self._raw, "rowcount", -1))

    # ---- 生命周期 ----

    def close(self) -> None:
        self._raw.close()

    def __iter__(self) -> Iterator[CaseInsensitiveRow]:
        return iter(self.fetchall())

    def __enter__(self) -> DialectCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
