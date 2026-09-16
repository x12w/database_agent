"""直接问数据库的几个小查询。

`/api/tables`、`/api/databases/{id}/status` 都需要"这个库里有哪些表、多少列"。
历史实现把这段逻辑写在路由里并按库名分支（`if is_dm … elif is_pg …`），
v4 的库类型分支只允许存在于 `db/dialects/` 里，所以这里统一走
`Dialect.sql_list_tables` / `sql_list_columns`，新增国产库无需改本文件。

这些查询**不查结构缓存**：表筛选界面的职责正是让用户看到库里的每一张表，
拿被裁剪过的缓存会让"筛选之后其他表就消失了"。
"""

from __future__ import annotations

import logging
from collections import Counter

from dbchat.api.models import TableInfo
from dbchat.db.executor import SqlExecutor
from dbchat.di import AppContext
from dbchat.domain.profile import DatabaseProfile
from dbchat.domain.schema import CatalogScope

logger = logging.getLogger(__name__)

__all__ = ["list_tables"]


async def list_tables(ctx: AppContext, profile: DatabaseProfile) -> list[TableInfo]:
    """列出库里的全部表（名称、近似行数、列数、注释）。

    Raises:
        DbError: 连不上库或查询失败 —— 文案已是中文，调用方直接展示。
    """
    # 不限制范围：这是"库里有哪些表"，不是"本次会话能看哪些表"。
    scope = CatalogScope()
    async with ctx.lease(profile) as lease:
        executor = SqlExecutor(lease, ctx.settings)
        rows = await executor.fetch_all(lease.dialect.sql_list_tables(scope))
        column_rows = await executor.fetch_all(lease.dialect.sql_list_columns(scope))

    column_counts = Counter(
        str(row.get("table_name") or "") for row in column_rows if row.get("table_name")
    )
    tables: list[TableInfo] = []
    for row in rows:
        name = str(row.get("table_name") or "")
        if not name:
            continue
        tables.append(
            TableInfo(
                name=name,
                row_count=_as_int(row.get("table_rows")),
                column_count=column_counts.get(name, 0),
                comment=str(row.get("table_comment") or ""),
            )
        )
    return tables


def _as_int(value: object) -> int:
    """行数可能是 None / Decimal / 字符串，统一成 int。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
