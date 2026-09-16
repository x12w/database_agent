"""数据库结构（schema）的领域模型。

纯数据，无 IO。由 `dbchat.db.introspect` 从各方言方言内省 SQL 构建，
被 agent 提示词渲染、SQL 校验、schema 缓存共同使用。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ColumnValueType(StrEnum):
    """列的取值形态 —— 帮助 agent 判断编码列、空列。"""

    ALL_NULL = "all_null"
    NUMERIC = "numeric"
    CODED = "coded"  # 短 ASCII 码值，如 '01'/'A'，业务含义需人工确认
    CHINESE = "chinese"
    TEXT = "text"
    BOOLEAN_LIKE = "boolean_like"
    DATETIME = "datetime"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ColumnMeta:
    """一列的结构与抽样观察。"""

    name: str
    data_type: str
    nullable: bool = True
    column_type: str = ""
    comment: str = ""
    default: Any = None
    is_primary_key: bool = False
    #: 抽样得到的语义画像（可能为空，取决于是否加载了详细缓存）。
    value_type: ColumnValueType | None = None
    null_ratio: float | None = None
    sample_values: list[str] = field(default_factory=list)

    def is_effectively_empty(self, threshold: float = 0.999) -> bool:
        """该列是否基本全为 NULL（或抽样值全空）。"""
        if self.null_ratio is not None:
            return self.null_ratio >= threshold
        return bool(self.sample_values) and all(v in ("", "None", "NULL") for v in self.sample_values)


@dataclass(slots=True)
class ForeignKeyMeta:
    """一条外键关系。"""

    table: str
    column: str
    ref_table: str
    ref_column: str
    constraint_name: str = ""

    def describe(self) -> str:
        return f"{self.table}.{self.column} -> {self.ref_table}.{self.ref_column}"


@dataclass(slots=True)
class IndexMeta:
    """一个索引。"""

    name: str
    columns: list[str] = field(default_factory=list)
    unique: bool = False
    is_primary: bool = False


@dataclass(slots=True)
class TableMeta:
    """一张表。"""

    name: str
    comment: str = ""
    #: None 表示"尚未统计"（与"确实为 0 行"区分开）。
    row_count: int | None = None
    columns: list[ColumnMeta] = field(default_factory=list)
    foreign_keys: list[ForeignKeyMeta] = field(default_factory=list)
    indexes: list[IndexMeta] = field(default_factory=list)
    #: 抽样行；fast 模式下为空，后续按需加载。
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    #: 该表结构是否已被后台详细探索覆盖。
    detailed: bool = False
    #: 近似行数不可信（如达梦未收集统计信息时）。
    row_count_approx: bool = True

    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> ColumnMeta | None:
        lowered = name.lower()
        for c in self.columns:
            if c.name.lower() == lowered:
                return c
        return None


@dataclass(slots=True)
class CatalogScope:
    """一次查询允许看到的表范围。

    取代历史上两个会跨会话泄漏的模块级全局变量
    （`schema_cache._active_table_filter` 与 `agent_tools._active_table_filter`）。
    不可变，并可作为缓存键的一部分。
    """

    #: None 表示"全部表"；frozenset 表示只允许这些表。
    tables: frozenset[str] | None = None

    def allows(self, table: str) -> bool:
        if self.tables is None:
            return True
        lowered = table.lower()
        return any(t.lower() == lowered for t in self.tables)

    def filter_names(self, names: list[str]) -> list[str]:
        return [n for n in names if self.allows(n)]

    def key(self) -> str:
        """稳定且短的缓存键后缀。"""
        if self.tables is None:
            return "all"
        digest = hashlib.sha1("\x00".join(sorted(t.lower() for t in self.tables)).encode()).hexdigest()
        return digest[:12]


@dataclass(slots=True)
class SchemaSnapshot:
    """一个数据库在某一时刻的完整结构快照。"""

    db_name: str
    tables: dict[str, TableMeta] = field(default_factory=dict)
    foreign_keys: list[ForeignKeyMeta] = field(default_factory=list)
    captured_at: float = 0.0
    scope: CatalogScope = field(default_factory=CatalogScope)
    #: 该快照是否由过期缓存恢复而来（需提示可能落后于实际库结构）。
    stale: bool = False
    #: 采集时各方言自报的检查点，用于新鲜度判断。
    checksums: dict[str, str] = field(default_factory=dict)

    @property
    def table_names(self) -> list[str]:
        return list(self.tables.keys())

    @property
    def table_count(self) -> int:
        return len(self.tables)

    def get_table(self, name: str) -> TableMeta | None:
        if name in self.tables:
            return self.tables[name]
        lowered = name.lower()
        for tname, meta in self.tables.items():
            if tname.lower() == lowered:
                return meta
        return None

    def resolve_table_name(self, name: str) -> str | None:
        """把可能是任意大小写的表名解析成快照中的真实表名。"""
        if name in self.tables:
            return name
        lowered = name.lower()
        for tname in self.tables:
            if tname.lower() == lowered:
                return tname
        return None

    def to_json_dict(self) -> dict[str, Any]:
        """序列化用于磁盘缓存。

        刻意用手写而非 dataclasses.asdict：字段增删时旧缓存必须能被识别为
        不兼容并丢弃，而不是静默产生缺失字段的对象。
        """
        return {
            "format_version": SCHEMA_FORMAT_VERSION,
            "db_name": self.db_name,
            "captured_at": self.captured_at,
            "scope": sorted(self.scope.tables) if self.scope.tables is not None else None,
            "checksums": self.checksums,
            "foreign_keys": [
                {
                    "table": fk.table,
                    "column": fk.column,
                    "ref_table": fk.ref_table,
                    "ref_column": fk.ref_column,
                    "constraint_name": fk.constraint_name,
                }
                for fk in self.foreign_keys
            ],
            "tables": {
                name: {
                    "name": t.name,
                    "comment": t.comment,
                    "row_count": t.row_count,
                    "row_count_approx": t.row_count_approx,
                    "detailed": t.detailed,
                    "columns": [
                        {
                            "name": c.name,
                            "data_type": c.data_type,
                            "column_type": c.column_type,
                            "nullable": c.nullable,
                            "comment": c.comment,
                            "default": c.default if isinstance(c.default, (str, int, float, bool, type(None))) else str(c.default),
                            "is_primary_key": c.is_primary_key,
                            "value_type": c.value_type.value if c.value_type else None,
                            "null_ratio": c.null_ratio,
                            "sample_values": c.sample_values,
                        }
                        for c in t.columns
                    ],
                    "indexes": [
                        {"name": i.name, "columns": i.columns, "unique": i.unique, "is_primary": i.is_primary}
                        for i in t.indexes
                    ],
                    "foreign_keys": [
                        {
                            "table": fk.table,
                            "column": fk.column,
                            "ref_table": fk.ref_table,
                            "ref_column": fk.ref_column,
                            "constraint_name": fk.constraint_name,
                        }
                        for fk in t.foreign_keys
                    ],
                    "sample_rows": t.sample_rows,
                }
                for name, t in self.tables.items()
            },
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> SchemaSnapshot:
        """从磁盘缓存反序列化。

        Raises:
            ValueError: 缓存格式版本不匹配（调用方应当作"无缓存"处理）。
        """
        version = data.get("format_version")
        if version != SCHEMA_FORMAT_VERSION:
            raise ValueError(f"schema 缓存格式版本不匹配: {version!r} != {SCHEMA_FORMAT_VERSION}")

        def _fk(d: dict[str, Any]) -> ForeignKeyMeta:
            return ForeignKeyMeta(
                table=d.get("table", ""),
                column=d.get("column", ""),
                ref_table=d.get("ref_table", ""),
                ref_column=d.get("ref_column", ""),
                constraint_name=d.get("constraint_name", ""),
            )

        tables: dict[str, TableMeta] = {}
        for name, t in (data.get("tables") or {}).items():
            columns = [
                ColumnMeta(
                    name=c["name"],
                    data_type=c.get("data_type", ""),
                    column_type=c.get("column_type", ""),
                    nullable=c.get("nullable", True),
                    comment=c.get("comment", ""),
                    default=c.get("default"),
                    is_primary_key=c.get("is_primary_key", False),
                    value_type=ColumnValueType(c["value_type"]) if c.get("value_type") else None,
                    null_ratio=c.get("null_ratio"),
                    sample_values=list(c.get("sample_values") or []),
                )
                for c in (t.get("columns") or [])
            ]
            tables[name] = TableMeta(
                name=t.get("name", name),
                comment=t.get("comment", ""),
                row_count=t.get("row_count"),
                row_count_approx=t.get("row_count_approx", True),
                detailed=t.get("detailed", False),
                columns=columns,
                indexes=[
                    IndexMeta(
                        name=i.get("name", ""),
                        columns=list(i.get("columns") or []),
                        unique=i.get("unique", False),
                        is_primary=i.get("is_primary", False),
                    )
                    for i in (t.get("indexes") or [])
                ],
                foreign_keys=[_fk(f) for f in (t.get("foreign_keys") or [])],
                sample_rows=list(t.get("sample_rows") or []),
            )

        scope_tables = data.get("scope")
        return cls(
            db_name=data.get("db_name", ""),
            tables=tables,
            foreign_keys=[_fk(f) for f in (data.get("foreign_keys") or [])],
            captured_at=float(data.get("captured_at") or 0.0),
            scope=CatalogScope(frozenset(scope_tables) if scope_tables is not None else None),
            checksums=dict(data.get("checksums") or {}),
        )


#: 磁盘缓存格式版本。字段不兼容变更时必须递增，旧缓存会被丢弃重建。
SCHEMA_FORMAT_VERSION = 2


def scope_cache_key(profile_id: str, scope: CatalogScope) -> str:
    """schema 缓存的完整键。

    把 scope 纳入键是关键：历史上筛选后的快照会覆盖全量快照，
    导致重启后一个无筛选请求拿到被裁剪的 schema，把缺失的表报成"不存在"。
    """
    return f"{profile_id}:{scope.key()}"


def stable_hash(obj: Any) -> str:
    """对任意可 JSON 化的对象求稳定摘要。"""
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()
