"""方言注册表 —— 新增国产数据库的唯一接入点。

用法::

    from dbchat.db.dialects import get_dialect
    dialect = get_dialect(DialectKind.DAMENG)

新增一个库的成本应当是：新建一个模块 + 加一行注册，不改任何其他文件。
`test_all_registered_dialects_are_complete` 会强制这一点。
"""

from __future__ import annotations

from typing import Callable, TypeVar

from dbchat.domain.errors import DbError, DbErrorKind
from dbchat.domain.profile import DialectKind
from dbchat.db.dialects.base import Dialect

_REGISTRY: dict[DialectKind, Dialect] = {}
_PENDING: dict[DialectKind, type[Dialect]] = {}

T = TypeVar("T", bound=Dialect)


def register_dialect(cls: type[T]) -> type[T]:
    """类装饰器：注册一个方言实现（惰性实例化）。"""
    kind = getattr(cls, "kind", None)
    if kind is None:
        raise TypeError(f"{cls.__name__} 未声明 kind")
    _PENDING[kind] = cls
    _REGISTRY.pop(kind, None)
    return cls


def get_dialect(kind: DialectKind) -> Dialect:
    """取得方言实例（进程内单例，方言对象本身无状态）。

    Raises:
        DbError: 该类型尚未实现时，给出明确的"如何接入"提示。
    """
    cached = _REGISTRY.get(kind)
    if cached is not None:
        return cached

    cls = _PENDING.get(kind)
    if cls is None:
        _load_builtin()
        cls = _PENDING.get(kind)

    if cls is None:
        implemented = ", ".join(sorted(k.value for k in _PENDING))
        raise DbError(
            DbErrorKind.UNSUPPORTED,
            user_message=(
                f"暂不支持 {kind.value} 数据库。"
                f"已实现: {implemented}。"
                f"接入方式：在 dbchat/db/dialects/ 下新建一个 Dialect 子类并用 "
                f"@register_dialect 注册。"
            ),
        )

    instance = cls()
    _REGISTRY[kind] = instance
    return instance


def registered_kinds() -> frozenset[DialectKind]:
    """当前已注册的方言类型（会先触发内置方言加载）。"""
    _load_builtin()
    return frozenset(_PENDING)


def dialect_factory() -> Callable[[DialectKind], Dialect]:
    """供依赖注入使用的工厂。"""
    return get_dialect


_builtin_loaded = False


def _load_builtin() -> None:
    """导入内置方言模块，触发注册。

    惰性导入是为了让"只想用 MySQL"的场景不必加载 dmPython/psycopg2。
    """
    global _builtin_loaded
    if _builtin_loaded:
        return
    _builtin_loaded = True

    from dbchat.db.dialects import mysql as _mysql  # noqa: F401
    from dbchat.db.dialects import postgres as _pg  # noqa: F401
    from dbchat.db.dialects import dameng as _dm  # noqa: F401


__all__ = ["Dialect", "get_dialect", "register_dialect", "registered_kinds"]
