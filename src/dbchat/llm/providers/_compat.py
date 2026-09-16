"""SDK 参数兼容垫片。

各家 SDK 的方法签名会随版本漂移：本环境里的 `anthropic` 1.6.0 已经把
`temperature` 从 `messages.create()/stream()` 的签名里去掉了，硬传就是
`TypeError: got an unexpected keyword argument 'temperature'` —— 整个
provider 直接不可用。

这里在调用前按签名过滤一次：SDK 认的参数照常传，不认的丢掉并记一条
debug 日志。这样新旧 SDK 都能跑，也不会静默改变参数语义之外的东西。
"""

from __future__ import annotations

import inspect
import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)


@lru_cache(maxsize=16)
def _accepted_parameters(func: Any) -> frozenset[str]:
    """方法接受的形参名集合（拿不到签名时返回空集）。"""
    try:
        return frozenset(inspect.signature(func).parameters)
    except (TypeError, ValueError):  # pragma: no cover - C 实现的可调用对象
        return frozenset()


def supported_kwargs(func: Any, **candidates: Any) -> dict[str, Any]:
    """只保留 `func` 真正接受的参数。

    拿不到签名（内建/装饰过的可调用对象）时原样返回 —— 宁可让 SDK 自己
    报错，也不要因为探测失败就把参数全部丢掉。
    """
    accepted = _accepted_parameters(func)
    if not accepted:
        return dict(candidates)
    kept = {name: value for name, value in candidates.items() if name in accepted}
    dropped = sorted(set(candidates) - set(kept))
    if dropped:
        logger.debug(
            "%s 不接受参数 %s，已忽略", getattr(func, "__qualname__", func), ", ".join(dropped)
        )
    return kept


__all__ = ["supported_kwargs"]
