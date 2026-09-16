"""数据库访问层。

对外只暴露方言无关的接口；`if db_type == ...` 不得出现在本包之外。
"""

from dbchat.db.dialects import Dialect, get_dialect, register_dialect, registered_kinds

__all__ = ["Dialect", "get_dialect", "register_dialect", "registered_kinds"]
