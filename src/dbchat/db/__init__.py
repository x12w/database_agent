"""数据库访问层。

对外只暴露方言无关的接口；"按库类型分支"的代码不得出现在本包之外。

分层（自下而上）::

    dialects/   方言子类（"按库类型分支"的唯一所在地）
    drivers.py  驱动装载与错误归类
    sqltext.py  字面量感知的 SQL 文本扫描
    cursor.py   统一游标与行映射
    connection.py 按 profile 的连接注册表
    introspect.py 从方言 SQL 构建 SchemaSnapshot
    guard.py    只读 / 限流 / 表列校验
    executor.py 校验->执行->截断的门面
"""

from dbchat.db.connection import (
    ConnectionLease,
    ConnectionManager,
    ConnectionProbe,
    get_connection_manager,
)
from dbchat.db.dialects import Dialect, get_dialect, register_dialect, registered_kinds
from dbchat.db.executor import SqlExecutor, SqlRunner, run_query
from dbchat.db.guard import GuardResult, inspect
from dbchat.db.introspect import build_snapshot, refresh_row_counts

__all__ = [
    "ConnectionLease",
    "ConnectionManager",
    "ConnectionProbe",
    "Dialect",
    "GuardResult",
    "SqlExecutor",
    "SqlRunner",
    "build_snapshot",
    "get_connection_manager",
    "get_dialect",
    "inspect",
    "refresh_row_counts",
    "register_dialect",
    "registered_kinds",
    "run_query",
]
