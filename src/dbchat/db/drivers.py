"""驱动装载与错误归类 —— 唯一允许碰"驱动私货"的地方。

历史实现把这些东西散在 `app/database/backends/dameng_backend.py` 里：
LD_LIBRARY_PATH 拼装、Nix store 里 `libstdc++.so.6` 的 ctypes 预加载、
以及用**报文子串**判断错误种类（`if "通讯" in msg`）。三者的共同问题是
"猜"：路径写死 `/nix/store`，错误种类靠中文关键字。

这里的做法：

* 路径不再写死 —— 先看 `NIX_STORE_DIR` / `NIX_STORE`，再由
  `ctypes.util.find_library` 兜底，最后才回落到 `/nix/store`（仅当它存在）。
* 错误归类优先用**异常类型 + 驱动错误码**：pymysql 的 `errno`、
  psycopg2 的 SQLSTATE（`pgcode`）、dmPython 的 `DmError.code`。
  只有在驱动什么都不给（典型的是 psycopg2 连接阶段，`pgcode` 为 None）
  时才回落到报文匹配，且匹配表是本模块里的常量，不是散落的条件分支。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import sys
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from dbchat.domain.errors import DbError, DbErrorKind

__all__ = [
    "classify_dmpython",
    "classify_psycopg2",
    "classify_pymysql",
    "driver_paramstyle",
    "driver_placeholder",
    "ensure_dameng_env",
    "find_nix_library",
    "load_dmpython",
    "load_psycopg2",
    "load_pymysql",
    "log_ping_failure",
    "nix_store_dir",
    "preload_libstdcpp",
]

logger = logging.getLogger(__name__)

#: 达梦客户端库的常见安装目录（按优先级）。
_DM_LIB_DIRS = (
    ("$DM_HOME", "bin"),
    ("$DM_HOME", "drivers/dpi"),
    ("/opt/dmdbms", "bin"),
    ("/home/dmdba/dmdbms", "bin"),
)

#: DB-API 2.0 的 paramstyle -> SQL 里实际的占位符写法。
_PARAMSTYLE_MARKERS: dict[str, str] = {
    "qmark": "?",
    "format": "%s",
    "pyformat": "%s",
    "named": ":name",
    "numeric": ":1",
}

#: 缺什么库就找什么。soname -> nix 包名前缀。
_NIX_LIBRARY_PACKAGES: dict[str, str] = {
    "libstdc++.so.6": "gcc-",
    "libgcc_s.so.1": "gcc-",
}

#: pymysql / MySQL 服务端错误码 -> 分类。
_MYSQL_ERROR_KINDS: dict[int, DbErrorKind] = {
    1044: DbErrorKind.PERMISSION,   # access denied for user to database
    1045: DbErrorKind.AUTH,         # access denied (bad password)
    1049: DbErrorKind.NOT_FOUND,    # unknown database
    1054: DbErrorKind.SYNTAX,       # unknown column
    1064: DbErrorKind.SYNTAX,       # parse error
    1142: DbErrorKind.PERMISSION,   # table access denied
    1146: DbErrorKind.NOT_FOUND,    # table doesn't exist
    1205: DbErrorKind.TIMEOUT,      # lock wait timeout
    1227: DbErrorKind.PERMISSION,   # access denied (privilege)
    1317: DbErrorKind.TIMEOUT,      # query interrupted
    2002: DbErrorKind.NETWORK,      # can't connect via socket
    2003: DbErrorKind.NETWORK,      # can't connect to server
    2006: DbErrorKind.NETWORK,      # server gone away
    2013: DbErrorKind.NETWORK,      # lost connection during query
    3024: DbErrorKind.TIMEOUT,      # max_execution_time exceeded
}

#: PostgreSQL SQLSTATE -> 分类。
_PG_SQLSTATE_KINDS: dict[str, DbErrorKind] = {
    "08000": DbErrorKind.NETWORK,
    "08003": DbErrorKind.NETWORK,
    "08006": DbErrorKind.NETWORK,
    "28000": DbErrorKind.AUTH,
    "28P01": DbErrorKind.AUTH,
    "3D000": DbErrorKind.NOT_FOUND,
    "3F000": DbErrorKind.NOT_FOUND,
    "42501": DbErrorKind.PERMISSION,
    "42601": DbErrorKind.SYNTAX,
    "42703": DbErrorKind.SYNTAX,
    "42P01": DbErrorKind.NOT_FOUND,
    "53300": DbErrorKind.NETWORK,
    "55P03": DbErrorKind.TIMEOUT,
    "57014": DbErrorKind.TIMEOUT,
}

#: psycopg2 连接阶段拿不到 SQLSTATE（libpq 直接失败），只能看报文。
#: 这些串来自 libpq 自身的报错模板，不随语言环境变化。
_PG_CONNECTION_HINTS: tuple[tuple[str, DbErrorKind], ...] = (
    ("password authentication failed", DbErrorKind.AUTH),
    ("no password supplied", DbErrorKind.AUTH),
    ("authentication failed", DbErrorKind.AUTH),
    ("role", DbErrorKind.AUTH),
    ("database", DbErrorKind.NOT_FOUND),  # FATAL: database "x" does not exist
    ("does not exist", DbErrorKind.NOT_FOUND),
    ("could not connect", DbErrorKind.NETWORK),
    ("connection refused", DbErrorKind.NETWORK),
    ("could not translate host name", DbErrorKind.NETWORK),
    ("timeout expired", DbErrorKind.NETWORK),
    ("no pg_hba.conf entry", DbErrorKind.AUTH),
)

#: 达梦错误码 -> 分类（取自 DmError.code，非报文匹配）。
_DM_ERROR_KINDS: dict[int, DbErrorKind] = {
    -2501: DbErrorKind.AUTH,        # 用户名或密码错误
    -2106: DbErrorKind.NOT_FOUND,   # 无效的表或视图名
    -2101: DbErrorKind.NOT_FOUND,   # 无效的对象名
    -2111: DbErrorKind.SYNTAX,      # 无效的列名 —— 让上层重写 SQL 而不是放弃
    -2007: DbErrorKind.SYNTAX,      # 语法分析出错
    -2001: DbErrorKind.SYNTAX,
    -70019: DbErrorKind.NETWORK,    # 通讯失败
    -70028: DbErrorKind.NETWORK,    # 创建 SOCKET 连接失败
    -6001: DbErrorKind.PERMISSION,
    -6103: DbErrorKind.UNKNOWN,     # 除 0
}

#: MySQL 客户端侧错误码（连接阶段 2003 掩盖了真实 errno 时用报文兜底）。
_MYSQL_CONNECTION_HINTS: tuple[tuple[str, DbErrorKind], ...] = (
    ("unknown database", DbErrorKind.NOT_FOUND),
    ("can't connect to mysql server on", DbErrorKind.NETWORK),
    ("access denied", DbErrorKind.AUTH),
    ("timed out", DbErrorKind.NETWORK),
)


# ---------------------------------------------------------------- 环境准备


def nix_store_dir() -> Path | None:
    """Nix store 根目录。

    先读环境变量（`NIX_STORE_DIR` / `NIX_STORE`，Nix 安装位置不一定在
    `/nix/store`），再退回约定位置 —— 且只在它确实存在时才承认。
    """
    for key in ("NIX_STORE_DIR", "NIX_STORE"):
        raw = os.environ.get(key)
        if raw:
            candidate = Path(raw)
            if candidate.is_dir():
                return candidate
    default = Path("/nix/store")
    return default if default.is_dir() else None


def find_nix_library(soname: str) -> Path | None:
    """定位 `soname` 的绝对路径，找不到返回 None。

    先问 `find_library`：它若返回绝对路径就直接用；若只返回裸名，
    说明动态加载器本来就找得到，无需预加载，返回 None。
    再按包名前缀在 store 里扫（store 位置由 `nix_store_dir` 决定，
    不写死 `/nix/store`）。
    """
    probe = soname.split(".so")[0].removeprefix("lib")
    found = ctypes.util.find_library(probe)
    if found and os.path.isabs(found):
        path = Path(found)
        if path.is_file():
            return path

    store = nix_store_dir()
    if store is None:
        return None
    prefix = _NIX_LIBRARY_PACKAGES.get(soname, "")
    patterns = [f"*-{prefix}*/lib/{soname}" if prefix else f"*/lib/{soname}", f"*-legacy-libs/lib/{soname}"]
    for pattern in patterns:
        matches = sorted(store.glob(pattern))
        if matches:
            return matches[-1]
    return None


def preload_libstdcpp() -> Path | None:
    """需要时用 RTLD_GLOBAL 预加载 `libstdc++.so.6`。

    Returns:
        实际加载的路径；不需要或加载失败时为 None（失败会记 warning）。
    """
    path = find_nix_library("libstdc++.so.6")
    if path is None:
        return None
    try:
        ctypes.CDLL(str(path), ctypes.RTLD_GLOBAL)
    except OSError as exc:
        # 预加载失败不致命 —— 记录后继续，让真正的 import 报出原始错误。
        logger.warning("预加载 %s 失败: %s", path, exc)
        return None
    logger.debug("已预加载 %s", path)
    return path


def ensure_dameng_env() -> list[str]:
    """把达梦客户端库目录补进 `LD_LIBRARY_PATH`。

    非侵入：只做追加，不建软链、不改已有顺序。

    Returns:
        实际新增的目录列表。
    """
    added: list[str] = []
    candidates: list[str] = []
    for root, sub in _DM_LIB_DIRS:
        base = os.path.expandvars(root)
        if "$" in base:
            continue  # 环境变量未设置
        candidates.append(os.path.join(base, sub))

    existing = os.environ.get("LD_LIBRARY_PATH", "")
    parts = existing.split(os.pathsep) if existing else []
    for directory in candidates:
        if os.path.isdir(directory) and directory not in parts:
            parts.append(directory)
            added.append(directory)
    if added:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(parts)
    return added


# ------------------------------------------------------------------ 装载


@lru_cache(maxsize=8)
def _import_module(name: str) -> Any:
    """导入模块；失败时抛 ImportError（异常不会被 lru_cache 缓存）。"""
    __import__(name)
    return sys.modules[name]


def driver_paramstyle(module: Any, default: str = "pyformat") -> str:
    """读取驱动自报的 paramstyle。

    不靠"猜"：`dmPython.paramstyle` 实测是 `qmark`，而 DM 的文档与
    历史代码都按 `?`/`%s` 混着写，只有问驱动才靠谱。
    """
    style = getattr(module, "paramstyle", None)
    return style if isinstance(style, str) and style else default


def driver_placeholder(loader: Callable[[], Any], default: str) -> str:
    """把驱动的 paramstyle 翻译成 SQL 里实际要写的占位符。

    Args:
        loader: 导入驱动的可调用对象（延迟导入，避免只为读一个字符串就
            把 dmPython 拉起来）。
        default: 驱动不可用时使用的占位符 —— 导入失败只记 warning，
            真正的报错留给 `connect`，这样"没装驱动"不会在构造 SQL 时
            就炸掉。
    """
    try:
        module = loader()
    except Exception as exc:
        logger.warning("无法导入驱动以读取 paramstyle，暂用默认占位符 %r: %s", default, exc)
        return default
    style = driver_paramstyle(module, default="")
    marker = _PARAMSTYLE_MARKERS.get(style)
    if marker is None:
        logger.warning("未知的驱动 paramstyle %r，暂用默认占位符 %r", style, default)
        return default
    return marker


def load_pymysql() -> Any:
    """导入 PyMySQL。

    Raises:
        DbError: 未安装时给出安装提示。
    """
    try:
        return _import_module("pymysql")
    except ImportError as exc:
        raise DbError(
            DbErrorKind.UNSUPPORTED,
            detail=str(exc),
            user_message="未安装 MySQL 驱动 pymysql，请执行: pip install pymysql",
            cause=exc,
        ) from exc


def load_psycopg2() -> Any:
    """导入 psycopg2（并确保 `psycopg2.extras` 可用）。

    `extras` 是子模块，`import psycopg2` 并不会把它拉进来；
    真库上第一次 `create_cursor` 会报 `module 'psycopg2' has no attribute
    'extras'`。在装载处一次性解决，别让调用方各自记得再 import 一次。

    Raises:
        DbError: 未安装时给出安装提示。
    """
    try:
        _import_module("psycopg2.extras")
        return _import_module("psycopg2")
    except ImportError as exc:
        raise DbError(
            DbErrorKind.UNSUPPORTED,
            detail=str(exc),
            user_message="未安装 PostgreSQL 驱动 psycopg2，请执行: pip install psycopg2-binary",
            cause=exc,
        ) from exc


@lru_cache(maxsize=1)
def load_dmpython() -> Any:
    """导入 dmPython，必要时先补齐动态库环境。

    达梦的原生扩展依赖 `libstdc++.so.6` 与 DPI 客户端库，在 NixOS 等
    非常规发行版上默认找不到。这里做两级兜底：先补 `LD_LIBRARY_PATH`，
    仍失败则用 ctypes 预加载 `libstdc++.so.6`。

    Raises:
        DbError: 驱动不可用，附带可操作的排查清单。
    """
    ensure_dameng_env()
    try:
        return _import_module("dmPython")
    except ImportError as exc:
        logger.debug("dmPython 首次导入失败（%s），尝试预加载 libstdc++", exc)

    preload_libstdcpp()
    try:
        return _import_module("dmPython")
    except ImportError as exc:
        raise DbError(
            DbErrorKind.UNSUPPORTED,
            detail=str(exc),
            user_message=(
                "无法加载达梦驱动 dmPython，请检查:\n"
                "  1. 是否已安装: pip install dmPython\n"
                "  2. DM_HOME 是否指向达梦安装目录\n"
                "  3. LD_LIBRARY_PATH 是否包含 $DM_HOME/bin\n"
                "  4. libstdc++.so.6 是否可用 (ldconfig -p | grep libstdc++)"
            ),
            cause=exc,
        ) from exc
    except OSError as exc:
        # 原生扩展加载时的 OSError（缺 so、符号不匹配）同样要给出可操作提示。
        raise DbError(
            DbErrorKind.UNSUPPORTED,
            detail=str(exc),
            user_message=f"达梦驱动加载失败: {exc}",
            cause=exc,
        ) from exc


# -------------------------------------------------------------- 错误归类


def _mysql_error_code(exc: BaseException) -> int | None:
    """pymysql 把错误码放在 `args[0]`。"""
    if exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    errno = getattr(exc, "errno", None)
    return errno if isinstance(errno, int) else None


def _dm_error_code(exc: BaseException) -> int | None:
    """dmPython 把 `DmError` 放在 `args[0]`，其 `.code` 是数值错误码。

    兼容少数版本直接抛 int 的情况。
    """
    if not exc.args:
        return None
    first = exc.args[0]
    if isinstance(first, int):
        return first
    code = getattr(first, "code", None)
    return code if isinstance(code, int) else None


def log_ping_failure(conn: Any, exc: BaseException) -> None:
    """探活失败：记 warning 后返回，由上层决定是否重建连接。

    这里**不能**静默 —— 历史上 `except Exception: pass` 让"连接其实早就
    断了"这件事在日志里完全看不到，排查时只能靠猜。
    """
    logger.warning("连接探活失败 (%s): %s: %s", type(conn).__name__, type(exc).__name__, exc)


def _match_hints(message: str, hints: tuple[tuple[str, DbErrorKind], ...]) -> DbErrorKind | None:
    lowered = message.lower()
    for needle, kind in hints:
        if needle in lowered:
            return kind
    return None


def classify_pymysql(exc: BaseException) -> DbErrorKind:
    """归类 PyMySQL 异常：错误码优先，报文仅在连接阶段兜底。"""
    name = type(exc).__name__
    code = _mysql_error_code(exc)
    if code is not None:
        kind = _MYSQL_ERROR_KINDS.get(code)
        if kind is not None:
            # 2003 会掩盖真实原因（如 1049 unknown database），报文里才有。
            if code in (2003, 2002):
                hinted = _match_hints(str(exc), _MYSQL_CONNECTION_HINTS)
                return hinted or kind
            return kind
    if name in ("InterfaceError", "OperationalError"):
        hinted = _match_hints(str(exc), _MYSQL_CONNECTION_HINTS)
        return hinted or DbErrorKind.NETWORK
    if name in ("ProgrammingError", "InternalError"):
        return DbErrorKind.SYNTAX
    if name == "IntegrityError":
        return DbErrorKind.UNKNOWN
    hinted = _match_hints(str(exc), _MYSQL_CONNECTION_HINTS)
    return hinted or DbErrorKind.UNKNOWN


def classify_psycopg2(exc: BaseException) -> DbErrorKind:
    """归类 psycopg2 异常：SQLSTATE 优先，连接阶段用 libpq 报文模板兜底。"""
    sqlstate = getattr(exc, "pgcode", None)
    if isinstance(sqlstate, str) and sqlstate:
        if sqlstate in _PG_SQLSTATE_KINDS:
            return _PG_SQLSTATE_KINDS[sqlstate]
        if sqlstate.startswith("08"):
            return DbErrorKind.NETWORK
        if sqlstate.startswith("28"):
            return DbErrorKind.AUTH
        if sqlstate.startswith("42"):
            return DbErrorKind.SYNTAX
        if sqlstate.startswith("53"):
            return DbErrorKind.NETWORK
        return DbErrorKind.UNKNOWN

    # 连接阶段：libpq 直接失败，没有 SQLSTATE。
    hinted = _match_hints(str(exc), _PG_CONNECTION_HINTS)
    if hinted is not None:
        return hinted
    name = type(exc).__name__
    if name in ("OperationalError", "InterfaceError"):
        return DbErrorKind.NETWORK
    if name == "ProgrammingError":
        return DbErrorKind.SYNTAX
    return DbErrorKind.UNKNOWN


def classify_dmpython(exc: BaseException) -> DbErrorKind:
    """归类 dmPython 异常：用 `DmError.code`，不做中文报文匹配。"""
    code = _dm_error_code(exc)
    if code is not None and code in _DM_ERROR_KINDS:
        return _DM_ERROR_KINDS[code]
    name = type(exc).__name__
    if name in ("InterfaceError", "OperationalError"):
        return DbErrorKind.NETWORK
    if name in ("ProgrammingError", "InternalError"):
        return DbErrorKind.SYNTAX
    if name == "NotSupportedError":
        return DbErrorKind.UNSUPPORTED
    return DbErrorKind.UNKNOWN
