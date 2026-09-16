"""候选列（含义不明的编码列）的启发式判定与优先级打分。

从两处合并而来：

* `app/agent/orchestrator.py:2157-2261`（主动探索里的"这个列含义不明吗"）
* `app/agent/background_explorer.py:24-70`（表探索优先级打分）

启发式规则**逐条保留**：短 ASCII 码值、小范围整数编码、枚举类型、
无样本时按列名猜编码列，以及"自明列不提问"的排除清单（`_id` / `_name` /
中文列名……）。放宽或收紧都会改变"哪些列会被问"，进而改变知识库的覆盖率。

这里只是把判定从"在探索循环里就地写"变成纯函数 —— 输入是快照里的表，
输出是候选列表，可单测。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from dbchat.domain.schema import ColumnMeta, ColumnValueType, TableMeta

__all__ = [
    "Candidate",
    "FAST_EXPLORE_TABLE_COUNT",
    "LARGE_DB_TABLE_THRESHOLD",
    "analyze_fk_counts",
    "find_candidates",
    "is_self_explanatory",
    "priority_order",
    "score_table",
]

#: 超过这个表数就进入"快速探索 + 后台补充"模式。
LARGE_DB_TABLE_THRESHOLD = 20
#: 首次快速探索时优先采样的表数。
FAST_EXPLORE_TABLE_COUNT = 10

#: 列名本身就说明用途 —— 不需要问管理员。
_SELF_EXPLANATORY_NAMES = frozenset(
    {
        "id", "name", "email", "phone", "address", "comment",
        "description", "remark", "note", "created_at", "updated_at",
        "create_time", "update_time", "date", "time",
    }
)
_SELF_EXPLANATORY_SUFFIXES = ("_id", "_name", "_time", "_date", "_at")
#: 浮点/高精度数值列不是编码列。
_FLOAT_TYPES = frozenset({"float", "double", "decimal", "numeric", "real", "double precision"})
_INTEGER_TYPES = frozenset({"tinyint", "smallint", "int", "bigint", "integer", "number"})
_STRING_TYPES = frozenset({"varchar", "char", "varchar2", "nvarchar2", "text", "nvarchar"})

#: 表名里出现这些片段 → 大概率是日志/临时表，降权。
_PENALTY_PATTERNS = (
    "log_", "audit_", "tmp_", "_bak", "_history", "_archive",
    "sys_", "__", "##", "sref_", "migrate_", "_log", "_tmp",
)
#: 表名里出现这些片段 → 大概率是业务主表，加权。
_BONUS_PATTERNS = (
    "employee", "user", "customer", "order", "product",
    "department", "company", "student", "course", "exam",
    "training", "salary", "contract", "project", "task",
)

#: 样本值里只要出现中文就算自明（中文值本身说明了含义）。
_CJK_START = "一"
_CJK_END = "鿿"

#: 小范围整数编码的判定阈值（历史值，不要动）。
_CODE_MAX_DISTINCT = 10
_CODE_MAX_VALUE = 100


@dataclass(slots=True)
class Candidate:
    """一个"含义不明、值得问管理员"的列。"""

    table: str
    column: str
    reason: str
    samples: list[str] = field(default_factory=list)
    column_type: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """去重键（大小写不敏感，与 `get_known_columns` 对齐）。"""
        return (self.table.lower(), self.column.lower())


def is_self_explanatory(name: str) -> bool:
    """列名本身是否已说明用途（不需要问管理员）。"""
    lowered = name.lower()
    if lowered in _SELF_EXPLANATORY_NAMES:
        return True
    if lowered.endswith(_SELF_EXPLANATORY_SUFFIXES):
        return True
    return bool(name) and _CJK_START <= name[0] <= _CJK_END


def find_candidates(
    tables: Mapping[str, TableMeta],
    *,
    known_columns: Iterable[tuple[str, str]] = (),
    sample_values: Mapping[tuple[str, str], list[str]] | None = None,
) -> list[Candidate]:
    """扫描所有表，找出需要向管理员确认的列。

    Args:
        tables: 结构快照里的表。
        known_columns: 已经问过（待答或已答）的 `(表名, 列名)` 小写集合。
        sample_values: 可选的、按 `(表名, 列名)` 给出的样本值覆盖
            （快照里没有抽样行时由探索层现取）。

    Returns:
        候选列表，顺序与表的遍历顺序一致。
    """
    known = {(table.lower(), column.lower()) for table, column in known_columns}
    overrides = sample_values or {}
    found: list[Candidate] = []

    for table_name, meta in tables.items():
        for column in meta.columns:
            if (table_name.lower(), column.name.lower()) in known:
                continue
            candidate = _judge(table_name, column, overrides.get((table_name, column.name)))
            if candidate is not None:
                found.append(candidate)
    return found


def _judge(table: str, column: ColumnMeta, samples: list[str] | None) -> Candidate | None:
    """单列判定。"""
    if is_self_explanatory(column.name):
        return None

    values = list(samples) if samples is not None else list(column.sample_values)
    values = [value for value in values if value not in (None, "")]
    data_type = (column.data_type or "").lower()
    name_lower = column.name.lower()

    if data_type in _FLOAT_TYPES:
        return None

    if data_type in ("enum", "set"):
        return _candidate(table, column, "枚举类型", values)

    if data_type in _INTEGER_TYPES and values:
        try:
            numbers = [int(value) for value in values]
        except ValueError:
            return None
        if len(set(numbers)) <= _CODE_MAX_DISTINCT and max(numbers) < _CODE_MAX_VALUE:
            return _candidate(table, column, f"小范围整数编码: {sorted(set(numbers))[:8]}", values)
        return None

    if data_type in _STRING_TYPES and values:
        if any(_has_cjk(value) for value in values):
            # 中文值本身说明了含义，不必问。
            return None
        if all(value.isdigit() for value in values) and len(values) > 1:
            if any(len(value) <= 4 for value in values):
                return _candidate(table, column, f"数字编码: {values[:5]}", values)
            return None
        if all(len(value) <= 10 and value.isascii() for value in values):
            return _candidate(table, column, f"短编码/缩写: {values[:5]}", values)
        return None

    if data_type in _STRING_TYPES and not values:
        # 没有样本，只能看列名像不像编码列。
        looks_coded = (len(column.name) <= 5 and column.name.isascii() and column.name.isupper()) or (
            "_" in column.name
            and any(part in name_lower for part in ("status", "type", "code", "level", "flag", "cd_"))
        )
        if looks_coded:
            return _candidate(table, column, f"列名疑似编码: {column.name}", values)
        return None

    # 快照画像给出"编码值"但类型不在上述清单里（如达梦的自定义类型）。
    if column.value_type is ColumnValueType.CODED and values:
        return _candidate(table, column, f"短编码/缩写: {values[:5]}", values)
    return None


def _candidate(table: str, column: ColumnMeta, reason: str, values: list[str]) -> Candidate:
    return Candidate(
        table=table,
        column=column.name,
        reason=reason,
        samples=[str(value)[:50] for value in values[:5]],
        column_type=column.column_type or column.data_type,
    )


def _has_cjk(text: str) -> bool:
    return any(_CJK_START <= char <= _CJK_END for char in text)


def score_table(table: str, meta: TableMeta, fk_counts: Mapping[str, int]) -> float:
    """表的探索优先级（越大越先探索）。

    历史实现（`background_explorer._score_table`）的权重原样保留：
    外键连接 +10/条、有数据 +5、业务主表 +3、日志/临时表 -10。
    """
    score = 0.0
    score += (fk_counts.get(f"in:{table}", 0) + fk_counts.get(f"out:{table}", 0)) * 10.0
    if meta.row_count:
        score += 5.0

    lowered = table.lower()
    if any(pattern in lowered for pattern in _PENALTY_PATTERNS):
        score -= 10.0
    elif any(pattern in lowered for pattern in _BONUS_PATTERNS):
        score += 3.0
    return score


def analyze_fk_counts(foreign_keys: Iterable) -> dict[str, int]:
    """统计每张表的入边/出边外键数量（`score_table` 的输入）。"""
    counts: dict[str, int] = {}
    for fk in foreign_keys:
        counts[f"out:{fk.table}"] = counts.get(f"out:{fk.table}", 0) + 1
        counts[f"in:{fk.ref_table}"] = counts.get(f"in:{fk.ref_table}", 0) + 1
    return counts


def priority_order(tables: Mapping[str, TableMeta], foreign_keys: Iterable) -> list[str]:
    """按优先级排序的表名（后台探索按这个顺序推进）。"""
    counts = analyze_fk_counts(foreign_keys)
    scored = [(name, score_table(name, meta, counts)) for name, meta in tables.items()]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [name for name, _ in scored]
