"""Dameng (达梦) database backend using dmPython.

Dameng is Oracle-compatible. Uses ALL_TABLES/ALL_TAB_COLUMNS/etc. instead of information_schema.

Requirements: pip install dmPython (requires libstdc++.so.6 in system library path)
"""

import os
import glob
import ctypes
from app.database.backends.base import DatabaseBackend


def _import_dmpython():
    """Import dmPython with minimal environment setup.

    Tries the standard import first. If that fails due to libstdc++ issues
    (common on NixOS), attempts to preload the library.

    Returns:
        dmPython module

    Raises:
        ImportError: with troubleshooting hints if dmPython cannot be imported
    """
    # Set up library paths (non-invasive — only adds to LD_LIBRARY_PATH)
    _setup_dm_env()

    # Try normal import
    try:
        import dmPython
        return dmPython
    except ImportError as e:
        msg = str(e)
        # Only handle libstdc++ / shared library errors with fallback
        if "libstdc" not in msg and "cannot open" not in msg and "No module named" not in msg:
            raise
        # Fall through to fallback
    except Exception as e:
        # Other errors (e.g., missing symbols) — try fallback
        pass

    # Fallback: preload libstdc++ (NixOS / non-standard environments)
    _preload_libstdcpp()

    try:
        import dmPython
        return dmPython
    except ImportError as e:
        raise ImportError(
            f"无法导入 dmPython 驱动: {e}\n\n"
            "请确保已安装达梦数据库驱动:\n"
            "  pip install dmPython\n\n"
            "如果已安装但仍报错，请检查:\n"
            "  1. DM_HOME 环境变量是否指向达梦安装目录\n"
            "  2. LD_LIBRARY_PATH 是否包含 $DM_HOME/bin\n"
            "  3. libstdc++.so.6 是否可用 (ldconfig -p | grep libstdc++)"
        ) from e


def _setup_dm_env():
    """Set environment variables for Dameng DPI libraries (non-invasive).

    Only modifies LD_LIBRARY_PATH — does NOT preload libraries or create symlinks.
    """
    dm_home = os.environ.get("DM_HOME", "")
    lib_dirs = []

    if dm_home:
        lib_dirs.extend([
            os.path.join(dm_home, "bin"),
            os.path.join(dm_home, "drivers", "dpi"),
        ])

    # Common install paths
    for p in ["/opt/dmdbms/bin", "/home/dmdba/dmdbms/bin"]:
        if os.path.isdir(p):
            lib_dirs.append(p)

    # Add to LD_LIBRARY_PATH
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    for d in lib_dirs:
        if d not in existing:
            existing = f"{d}:{existing}" if existing else d
    if existing:
        os.environ["LD_LIBRARY_PATH"] = existing


def _preload_libstdcpp():
    """Preload libstdc++.so.6 for NixOS and other non-standard environments.

    Only runs when the normal import of dmPython fails due to missing libstdc++.
    """
    lib_path = None
    for pattern in [
        "/nix/store/*-gcc-*-lib/lib/libstdc++.so.6",
        "/nix/store/*-legacy-libs/libstdc++.so.6",
    ]:
        matches = glob.glob(pattern)
        if matches:
            lib_path = matches[0]
            break
    if lib_path:
        try:
            ctypes.CDLL(lib_path, ctypes.RTLD_GLOBAL)
        except Exception:
            pass


class DamengBackend(DatabaseBackend):
    """Dameng database backend (Oracle-compatible)."""

    def connect(self, host, port, user, password, database, **kwargs):
        dmPython = _import_dmpython()
        timeout = kwargs.get("connect_timeout", 30)
        try:
            conn = dmPython.connect(
                server=host,
                port=port,
                user=user,
                password=password,
                login_timeout=timeout,
            )
        except Exception as e:
            msg = str(e)
            if "70019" in msg or "通讯" in msg or "通讯失败" in msg:
                raise ConnectionError(
                    f"达梦数据库网络通讯失败 ({host}:{port}): {msg}\n\n"
                    "请检查:\n"
                    "  1. 主机地址和端口是否正确\n"
                    "  2. 防火墙是否允许连接\n"
                    "  3. 达梦数据库服务是否已启动\n"
                    "  4. 网络是否可达 (ping {host})"
                ) from e
            elif "password" in msg.lower() or "login" in msg.lower() or "70019" in msg:
                raise ConnectionError(
                    f"达梦数据库登录失败 (用户: {user}): {msg}\n\n"
                    "请检查用户名和密码是否正确。"
                ) from e
            raise ConnectionError(
                f"达梦数据库连接失败 ({host}:{port}): {msg}"
            ) from e

        # If a specific schema/database is specified, set it
        if database:
            cur = conn.cursor()
            try:
                cur.execute(f'SET SCHEMA "{database}"')
            except Exception:
                pass
            finally:
                cur.close()
        return conn

    def ping(self, conn) -> bool:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM DUAL")
            cur.close()
            return True
        except Exception:
            return False

    def close(self, conn):
        try:
            conn.close()
        except Exception:
            pass

    def create_cursor(self, conn):
        return conn.cursor()

    def quote_identifier(self, name: str) -> str:
        return f'"{name}"'

    def placeholder(self) -> str:
        return "?"
