"""结构内省 —— 从 `Dialect.sql_*` 构建 `SchemaSnapshot`。

历史实现（`app/database/schema_cache.py:321-675`）里散着**七处三向分支**，
同一份列表查询有四份副本，改一处要同步三处。这里是一条**线性**代码路径：
方言差异全部落在 `Dialect.sql_*` 与 `DialectCapabilities` 上，本模块
没有任何"按库类型分支"的判断 —— 想确认这一点，看这个文件里有没有
方言名的字符串常量即可。

列名契约（每个方言的内省 SQL 都必须别名到这些名字，大小写不敏感）::

    表:      table_name, table_type, table_rows, table_comment
    列:      table_name, column_name, data_type, column_type, is_nullable,
             column_key, column_default, column_comment, ordinal_position
    外键:    constraint_name, table_name, column_name, ref_table_name,
             ref_column_name, delete_rule, update_rule, ordinal_position
    索引:    table_name, index_name, column_name, is_unique, is_primary,
             ordinal_position
    检查点:  object_name, fingerprint
    行数:    row_count
    表数量:  table_count
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from dbchat.db.dialects.base import Dialect, RowCountMode
from dbchat.domain.query import BoundSQL
from dbchat.domain.schema import (
    CatalogScope,
    ColumnMeta,
    ColumnValueType,
    ForeignKeyMeta,
    IndexMeta,
    SchemaSnapshot,
    TableMeta,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from dbchat.db.executor import SqlRunner

__all__ = [
    "build_snapshot",
    "count_tables_sql",
    "load_table_samples",
    "refresh_row_counts",
]

logger = logging.getLogger(__name__)

#: 精确计数最多补多少张表。
#:
#: `COUNT(*)` 在大表上是全表扫描，历史实现把它限制在 50 张（
#: `app/database/schema_cache.py:412`）以免首次加载超时。这里保留同样的
#: 保护，但把它收敛成一个具名常量而不是散落的魔法数字。
EXACT_COUNT_TABLE_CAP = 50

#: 进度回调：(阶段名, 已完成, 总数)。
ProgressFn = Callable[[str, int, int], None]


def count_tables_sql(dialect: Dialect, scope: CatalogScope) -> BoundSQL:
    """取"数表数量"的 SQL。

    方言可以只实现 `sql_count_tables`（更便宜的 COUNT），
    没有实现时退回表清单 SQL —— 这样新增方言不会被这条辅助路径绊住。
    """
    counter = getattr(dialect, "sql_count_tables", None)
    if callable(counter):
        return counter(scope)
    return dialect.sql_list_tables(scope)


async def build_snapshot(
    runner: SqlRunner,
    scope: CatalogScope,
    *,
    fast: bool = False,
    sample_rows: int = 3,
    progress: ProgressFn | None = None,
    exact_count_cap: int = EXACT_COUNT_TABLE_CAP,
) -> SchemaSnapshot:
    """采集一份结构快照。

    Args:
        runner: 能执行 `BoundSQL` 的执行器（`SqlExecutor`）。
        scope: 允许看到的表范围；不在此范围内的表会被丢弃，并被记入快照。
        fast: 快速模式 —— 跳过抽样与取值画像（大库首次加载用）。
        sample_rows: 每张表抽样的行数。
        progress: 进度回调。
        exact_count_cap: 精确补计数的表数上限。

    Returns:
        采集完成的 `SchemaSnapshot`。
    """
    dialect = runner.dialect
    tables = await _load_tables(runner, scope, dialect, progress)
    columns = await _load_columns(runner, dialect, scope, progress)
    _attach_columns(tables, columns)

    foreign_keys = await _load_foreign_keys(runner, dialect, scope, progress)
    _attach_foreign_keys(tables, foreign_keys)

    indexes = await _load_indexes(runner, dialect, scope, progress)
    _attach_indexes(tables, indexes)

    if not fast:
        await load_table_samples(runner, tables, limit=sample_rows, progress=progress)

    # 行数可信度由方言**自报**（能力声明），不是按库名分支 —— 达梦未收集统计信息时为假。
    if not dialect.capabilities.approx_row_count_reliable:
        await refresh_row_counts(runner, dialect, tables, scope, cap=exact_count_cap, progress=progress)

    checksums = await _load_checksums(runner, dialect, scope)

    return SchemaSnapshot(
        db_name=runner.profile.database or dialect.display_name,
        tables=tables,
        foreign_keys=foreign_keys,
        captured_at=time.time(),
        scope=scope,
        checksums=checksums,
    )


# ------------------------------------------------------------------ 采集


def _notify(progress: ProgressFn | None, stage: str, done: int, total: int) -> None:
    if progress is not None:
        try:
            progress(stage, done, total)
        except Exception as exc:  # noqa: BLE001 - 进度回调不该影响采集
            logger.warning("进度回调抛出异常 (stage=%s): %s: %s", stage, type(exc).__name__, exc)


async def _load_tables(
    runner: SqlRunner, scope: CatalogScope, dialect: Dialect, progress: ProgressFn | None
) -> dict[str, TableMeta]:
    rows = await runner.fetch_all(dialect.sql_list_tables(scope))
    tables: dict[str, TableMeta] = {}
    for row in rows:
        name = str(row.get("table_name") or "")
        if not name or not scope.allows(name):
            continue
        tables[name] = TableMeta(
            name=name,
            comment=str(row.get("table_comment") or ""),
            row_count=_as_int(row.get("table_rows")),
            row_count_approx=True,
        )
    _notify(progress, "tables", len(tables), len(tables))
    return tables


async def _load_columns(
    runner: SqlRunner, dialect: Dialect, scope: CatalogScope, progress: ProgressFn | None
) -> dict[str, list[ColumnMeta]]:
    rows = await runner.fetch_all(dialect.sql_list_columns(scope))
    grouped: dict[str, list[ColumnMeta]] = {}
    for row in rows:
        table = str(row.get("table_name") or "")
        if not table or not scope.allows(table):
            continue
        data_type = str(row.get("data_type") or "")
        column_type = str(row.get("column_type") or data_type)
        key = str(row.get("column_key") or "")
        grouped.setdefault(table, []).append(
            ColumnMeta(
                name=str(row.get("column_name") or ""),
                data_type=data_type,
                column_type=column_type,
                nullable=_as_bool(row.get("is_nullable"), default=True),
                comment=str(row.get("column_comment") or ""),
                default=row.get("column_default"),
                is_primary_key=key.upper() in ("PRI", "PRIMARY KEY"),
            )
        )
    _notify(progress, "columns", sum(len(v) for v in grouped.values()), len(grouped))
    return grouped


def _attach_columns(
    tables: dict[str, TableMeta], grouped: dict[str, list[ColumnMeta]]
) -> None:
    for table, columns in grouped.items():
        meta = tables.get(table) or _lookup_ci(tables, table)
        if meta is not None:
            meta.columns = columns


async def _load_foreign_keys(
    runner: SqlRunner, dialect: Dialect, scope: CatalogScope, progress: ProgressFn | None
) -> list[ForeignKeyMeta]:
    rows = await runner.fetch_all(dialect.sql_list_foreign_keys(scope))
    result: list[ForeignKeyMeta] = []
    for row in rows:
        table = str(row.get("table_name") or "")
        ref_table = str(row.get("ref_table_name") or "")
        if not table or not ref_table or not scope.allows(table):
            continue
        result.append(
            ForeignKeyMeta(
                table=table,
                column=str(row.get("column_name") or ""),
                ref_table=ref_table,
                ref_column=str(row.get("ref_column_name") or ""),
                constraint_name=str(row.get("constraint_name") or ""),
            )
        )
    _notify(progress, "foreign_keys", len(result), len(result))
    return result


def _attach_foreign_keys(tables: dict[str, TableMeta], foreign_keys: list[ForeignKeyMeta]) -> None:
    for fk in foreign_keys:
        meta = tables.get(fk.table) or _lookup_ci(tables, fk.table)
        if meta is not None:
            meta.foreign_keys.append(fk)


async def _load_indexes(
    runner: SqlRunner, dialect: Dialect, scope: CatalogScope, progress: ProgressFn | None
) -> dict[str, list[IndexMeta]]:
    """索引按 (表, 索引名) 聚合 —— 每行是索引里的一列。"""
    rows = await runner.fetch_all(dialect.sql_list_indexes(scope))
    grouped: dict[str, dict[str, IndexMeta]] = {}
    for row in rows:
        table = str(row.get("table_name") or "")
        name = str(row.get("index_name") or "")
        if not table or not name or not scope.allows(table):
            continue
        index = grouped.setdefault(table, {}).get(name)
        if index is None:
            index = IndexMeta(
                name=name,
                columns=[],
                unique=_as_bool(row.get("is_unique")),
                is_primary=_as_bool(row.get("is_primary")),
            )
            grouped[table][name] = index
        column = str(row.get("column_name") or "")
        if column and column not in index.columns:
            index.columns.append(column)
    _notify(progress, "indexes", sum(len(v) for v in grouped.values()), len(grouped))
    return {table: list(indexes.values()) for table, indexes in grouped.items()}


def _attach_indexes(tables: dict[str, TableMeta], grouped: dict[str, list[IndexMeta]]) -> None:
    for table, indexes in grouped.items():
        meta = tables.get(table) or _lookup_ci(tables, table)
        if meta is not None:
            meta.indexes = indexes
            # 主键信息以索引为准再补一次：有的方言列清单里给不出 PK 标记。
            for index in indexes:
                if index.is_primary:
                    for column in index.columns:
                        col = meta.column(column)
                        if col is not None:
                            col.is_primary_key = True


async def load_table_samples(
    runner: SqlRunner,
    tables: Mapping[str, TableMeta],
    *,
    limit: int = 3,
    only: Sequence[str] | None = None,
    progress: ProgressFn | None = None,
) -> int:
    """给表加载抽样行，并从样本推出列的取值画像。

    抽样 SQL 由 `Dialect.sql_sample` 提供（达梦会给出
    `FETCH FIRST n ROWS ONLY` 而不是 `LIMIT`）。

    样本行的键就是驱动返回的真实列名（达梦大写、MySQL 原样、PG 小写），
    与 `ColumnMeta.name` 一一对应 —— 消费方请用 `column.name` 取值，
    不要硬编码小写键。内存中的行是大小写不敏感的映射，但从磁盘缓存
    反序列化回来的是普通 dict，只有原始键。

    Returns:
        成功取到样本的表数。
    """
    dialect = runner.dialect
    allowed = None if only is None else {n.lower() for n in only}
    targets = [t for name, t in tables.items() if allowed is None or name.lower() in allowed]
    loaded = 0
    for done, table in enumerate(targets, start=1):
        if table.sample_rows:
            continue
        try:
            bound = dialect.sql_sample(table.name, table.column_names(), limit)
            rows = await runner.fetch_all(bound)
        except Exception as exc:  # noqa: BLE001 - 单表抽样失败不该中断整库内省
            logger.warning("抽样 %s 失败: %s: %s", table.name, type(exc).__name__, exc)
            _notify(progress, "samples", done, len(targets))
            continue
        table.sample_rows = list(rows)
        table.detailed = True
        if table.sample_rows:
            loaded += 1
            _profile_columns(table)
        _notify(progress, "samples", done, len(targets))
    return loaded


async def refresh_row_counts(
    runner: SqlRunner,
    dialect: Dialect,
    tables: dict[str, TableMeta],
    scope: CatalogScope,
    *,
    cap: int = EXACT_COUNT_TABLE_CAP,
    progress: ProgressFn | None = None,
) -> int:
    """给"统计信息不可信"的表补精确行数。

    达梦未收集统计信息时 `ALL_TABLES.NUM_ROWS` 整片为 0（本地容器实测
    16 张表全部为 0），不补 `COUNT(*)` 的话上层会以为库是空的。
    筛选范围内的表优先，最多补 `cap` 张，避免首次加载被全表扫描拖死。

    Returns:
        被修正的表数。
    """
    candidates = [t for t in tables.values() if not t.row_count]
    if not candidates:
        return 0
    if scope.tables is not None:
        priority = {t.lower() for t in scope.tables}
        candidates.sort(key=lambda t: (0 if t.name.lower() in priority else 1, t.name))
    else:
        candidates.sort(key=lambda t: t.name)

    fixed = 0
    for done, table in enumerate(candidates[:cap], start=1):
        try:
            bound = dialect.sql_row_count(table.name, RowCountMode.EXACT)
            rows = await runner.fetch_all(bound)
        except Exception as exc:  # noqa: BLE001 - 单表计数失败保留 0，但必须留痕
            logger.warning("精确计数 %s 失败: %s: %s", table.name, type(exc).__name__, exc)
            continue
        count = _as_int(rows[0].get("row_count")) if rows else None
        if count:
            table.row_count = count
            table.row_count_approx = False
            fixed += 1
        _notify(progress, "row_counts", done, min(len(candidates), cap))
    return fixed


async def _load_checksums(
    runner: SqlRunner, dialect: Dialect, scope: CatalogScope
) -> dict[str, str]:
    """把方言给出的 (对象名, 指纹) 行压成一个摘要。

    摘要只是"廉价提示"：`Dialect.checksum_is_reliable()` 返回 False 时，
    上层必须按 TTL 刷新并提供强制刷新入口，不能拿它当"结构未变"的证明。
    """
    rows = await runner.fetch_all(dialect.sql_checksum(scope))
    payload = "|".join(
        f"{row.get('object_name')}={row.get('fingerprint')}"
        for row in sorted(rows, key=lambda r: str(r.get("object_name") or ""))
    )
    return {"catalog_md5": hashlib.md5(payload.encode("utf-8")).hexdigest()}


# ------------------------------------------------------------- 值处理


def _lookup_ci(tables: Mapping[str, TableMeta], name: str) -> TableMeta | None:
    """按大小写不敏感的方式找表（达梦返回大写、MySQL 原样）。"""
    lowered = name.lower()
    for key, meta in tables.items():
        if key.lower() == lowered:
            return meta
    return None


def _as_int(value: Any) -> int | None:
    """尽力转 int；转不出来返回 None（"尚未统计"，不是 0）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.debug("无法把 %r 解析成整数", value)
        return None


def _as_bool(value: Any, default: bool = False) -> bool:
    """把各驱动的"布尔"统一成 bool。

    MySQL 给 0/1，PG 给 True/False，达梦给 'Y'/'N' —— `bool("N")` 是 True，
    直接用 `bool()` 会把"非唯一索引"读成"唯一索引"。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("y", "yes", "t", "true", "1", "uni"):
        return True
    if text in ("n", "no", "f", "false", "0", ""):
        return False
    return default


def _profile_columns(table: TableMeta) -> None:
    """从抽样行推出每列的取值画像（`ColumnMeta.value_type` 等）。"""
    if not table.sample_rows:
        return
    total = len(table.sample_rows)
    for column in table.columns:
        values = [row.get(column.name) for row in table.sample_rows]
        present = [v for v in values if v is not None and str(v) != ""]
        column.null_ratio = round(1 - len(present) / total, 3) if total else None
        column.value_type = _infer_value_type(present)
        column.sample_values = [str(v)[:60] for v in present[:3]]


def _infer_value_type(values: Sequence[Any]) -> ColumnValueType:
    """根据样本值判断取值形态。"""
    if not values:
        return ColumnValueType.ALL_NULL

    texts = [str(v).strip() for v in values]
    if all(_looks_numeric(t) for t in texts):
        return ColumnValueType.NUMERIC
    if all(_looks_datetime(v) for v in values):
        return ColumnValueType.DATETIME
    if all(t.lower() in ("0", "1", "y", "n", "t", "f", "true", "false", "是", "否") for t in texts):
        return ColumnValueType.BOOLEAN_LIKE
    if any(_contains_cjk(t) for t in texts):
        return ColumnValueType.CHINESE
    if all(len(t) <= 3 and t.isascii() for t in texts):
        # 短 ASCII 码值（'01'、'A'）：业务含义需要人工确认
        return ColumnValueType.CODED
    return ColumnValueType.TEXT


def _looks_numeric(text: str) -> bool:
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def _looks_datetime(value: Any) -> bool:
    import datetime

    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return True
    text = str(value)
    return len(text) >= 10 and text[:4].isdigit() and text[4] in "-/"


def _contains_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)

