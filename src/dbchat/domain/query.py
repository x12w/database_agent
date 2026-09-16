"""查询结果与可绑定 SQL 的领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence


class TypeFamily(StrEnum):
    """列的数据类型大类 —— 供 agent 判断能否聚合、能否画图。"""

    NUMERIC = "numeric"
    STRING = "string"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    BINARY = "binary"
    JSON = "json"
    OTHER = "other"


@dataclass(slots=True, frozen=True)
class BoundSQL:
    """一条 SQL 及其绑定参数。

    存在的意义是让"内省 SQL"和"用户 SQL"走同一条执行路径：
    内省 SQL 由方言生成、参数化绑定；用户 SQL 由 LLM 生成、无参数。
    """

    sql: str
    params: tuple[Any, ...] = ()

    def with_sql(self, sql: str) -> BoundSQL:
        return BoundSQL(sql=sql, params=self.params)


@dataclass(slots=True)
class QueryResult:
    """一次查询的结果。"""

    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    #: 结果被行数上限截断了（真实行数可能更多）。
    truncated: bool = False
    execution_ms: int = 0
    #: 查询过程中的非致命提示（如未加限流被自动补上 LIMIT）。
    notices: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.row_count == 0

    def column_families(self) -> dict[str, TypeFamily]:
        """根据首行数据的 Python 类型推断每列的大类。"""
        families: dict[str, TypeFamily] = {}
        for col in self.columns:
            families[col] = _family_of(self.rows[0].get(col) if self.rows else None)
        return families

    def to_prompt_text(self, max_rows: int = 50) -> str:
        """渲染成给 LLM 看的紧凑文本。

        列多、行多时按列裁剪，避免把上下文撑爆。
        """
        if not self.columns:
            return "（无结果）"
        if self.row_count == 0:
            return "（查询成功，但没有匹配的数据）"

        shown = self.rows[:max_rows]
        header = " | ".join(self.columns)
        lines = [header, "-" * min(len(header), 80)]
        for row in shown:
            lines.append(" | ".join(_stringify(row.get(c)) for c in self.columns))
        if self.row_count > len(shown):
            lines.append(f"... 另有 {self.row_count - len(shown)} 行未显示")
        return "\n".join(lines)


@dataclass(slots=True)
class Artifact:
    """一次查询/计算产生的可展示产物（图表等）。"""

    artifact_id: str
    kind: str  # "chart" | "table"
    url: str
    title: str = ""


def _family_of(value: Any) -> TypeFamily:
    if value is None:
        return TypeFamily.OTHER
    if isinstance(value, bool):
        return TypeFamily.BOOLEAN
    if isinstance(value, (int, float)):
        return TypeFamily.NUMERIC
    if isinstance(value, (bytes, bytearray, memoryview)):
        return TypeFamily.BINARY
    if isinstance(value, (dict, list)):
        return TypeFamily.JSON
    if hasattr(value, "year") and hasattr(value, "month"):
        return TypeFamily.DATETIME
    if isinstance(value, str):
        return TypeFamily.STRING
    # Decimal / datetime 等驱动特定类型
    module = type(value).__module__
    name = type(value).__name__.lower()
    if "decimal" in module or name == "decimal":
        return TypeFamily.NUMERIC
    if "datetime" in module or name in ("date", "time", "datetime", "timestamp"):
        return TypeFamily.DATETIME
    return TypeFamily.OTHER


def _stringify(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(value)}B>"
    text = str(value)
    return text if len(text) <= 200 else text[:197] + "..."


def rows_to_csv(columns: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> str:
    """把结果渲染成 CSV —— 供用户下载与计算沙箱读取。"""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(c) for c in columns])
    return buf.getvalue()
