"""Database connection manager.

Supports MySQL (PyMySQL) and PostgreSQL (psycopg2).
Uses backend abstraction for driver-specific operations.
"""

import pymysql
import pymysql.err
import time
from contextlib import contextmanager
from typing import Optional

from app.config import config, DatabaseProfile
from app.database.backends import get_backend


class _PGCursorWrapper:
    """Wraps a psycopg2 RealDictCursor to normalize column names.

    PostgreSQL lowercases unquoted identifiers. This wrapper:
    1. Stores both UPPERCASE and lowercase keys for case-insensitive access
    2. Supports dict-style access with either case
    """
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql, params=None):
        return self._cursor.execute(sql, params)

    def fetchall(self):
        return [self._normalize(r) for r in self._cursor.fetchall()]

    def fetchone(self):
        r = self._cursor.fetchone()
        return self._normalize(r) if r else None

    def fetchmany(self, size=None):
        return [self._normalize(r) for r in self._cursor.fetchmany(size)]

    @staticmethod
    def _normalize(row):
        if row is None:
            return None
        return _CaseInsensitiveDict(row)

    def close(self):
        self._cursor.close()

    def __iter__(self):
        return iter(self.fetchall())

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _TupleCursorWrapper:
    """Wraps a cursor that returns tuples to return dicts instead (for Dameng/Oracle).

    Also converts %s placeholders to ? (Dameng uses ? like Oracle).
    """

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    def execute(self, sql, params=None):
        # Dameng uses ? instead of %s
        if "%s" in sql:
            sql = sql.replace("%s", "?")
        if params:
            return self._cursor.execute(sql, params)
        return self._cursor.execute(sql)

    def _row_to_dict(self, row):
        if row is None:
            return None
        if not self._cursor.description:
            return row
        d = {}
        for i, col_desc in enumerate(self._cursor.description):
            key = col_desc[0].upper()
            d[key] = row[i] if i < len(row) else None
        return _CaseInsensitiveDict(d)

    def fetchall(self):
        return [self._row_to_dict(r) for r in self._cursor.fetchall()]

    def fetchone(self):
        return self._row_to_dict(self._cursor.fetchone())

    def fetchmany(self, size=None):
        return [self._row_to_dict(r) for r in self._cursor.fetchmany(size)]

    def close(self):
        self._cursor.close()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CaseInsensitiveDict(dict):
    """A dict that allows case-insensitive key access.
    Keys are stored in uppercase internally, but can be accessed with any case.
    """
    def __init__(self, d):
        super().__init__({k.upper(): v for k, v in d.items()})

    def __getitem__(self, key):
        return super().__getitem__(key.upper())

    def get(self, key, default=None):
        return super().get(key.upper(), default)

    def __contains__(self, key):
        return super().__contains__(key.upper())


class DatabaseConnection:
    """Manages database connections with auto-reconnect and backend switching."""

    def __init__(self):
        self._conn = None
        self._current_profile: Optional[DatabaseProfile] = None
        self._backend_type = "mysql"

    @property
    def current_profile(self) -> Optional[DatabaseProfile]:
        return self._current_profile

    @property
    def current_db_name(self) -> str:
        if self._current_profile:
            return self._current_profile.database
        return config.DB_NAME

    @property
    def db_type(self) -> str:
        if self._current_profile:
            return self._current_profile.db_type
        return "mysql"

    # ---- Connection ----

    def connect_to(self, profile: DatabaseProfile):
        """Connect to a database profile. Uses backend abstraction."""
        if self._conn and self._current_profile and self._current_profile.id != profile.id:
            self.close()

        self._current_profile = profile
        db_type = profile.db_type.lower()
        self._backend_type = db_type

        try:
            backend = get_backend(db_type)
            self._conn = backend.connect(
                host=profile.host, port=profile.port,
                user=profile.user, password=profile.password,
                database=profile.database,
                connect_timeout=config.DB_CONNECT_TIMEOUT,
            )
            return self._conn
        except ValueError as e:
            raise ConnectionError(str(e)) from e
        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to {profile.host}:{profile.port}/{profile.database}: {e}"
            ) from e

    def connect(self):
        if self._current_profile:
            return self.connect_to(self._current_profile)
        return self.connect_to(config.get_default_profile())

    @property
    def conn(self):
        if self._conn is None:
            return self.connect()
        try:
            backend = get_backend(self.db_type)
            if not backend.ping(self._conn):
                raise Exception("Ping failed")
        except Exception:
            self._conn = None
            return self.connect()
        return self._conn

    def test_connection(self, profile: DatabaseProfile) -> dict:
        """Test if a database profile can connect successfully."""
        db_type = profile.db_type.lower()

        if db_type not in ("mysql", "mariadb", "postgresql", "postgres", "pg", "dameng", "dm", "dm8"):
            return {"success": False, "message": f"暂不支持 {profile.db_type} 数据库类型。当前支持: MySQL/MariaDB/PostgreSQL/Dameng。", "tables": 0}

        try:
            if db_type in ("mysql", "mariadb"):
                return self._test_mysql(profile)
            elif db_type in ("dameng", "dm", "dm8"):
                return self._test_dameng(profile)
            else:
                return self._test_postgres(profile)
        except Exception as e:
            return {"success": False, "message": f"连接失败: {e}", "tables": 0}

    def _test_mysql(self, profile: DatabaseProfile) -> dict:
        try:
            c = pymysql.connect(
                host=profile.host, port=profile.port,
                user=profile.user, password=profile.password,
                database=profile.database,
                charset="utf8mb4", connect_timeout=config.DB_CONNECT_TIMEOUT,
                cursorclass=pymysql.cursors.DictCursor, autocommit=True,
            )
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'",
                        (profile.database,),
                    )
                    n = cur.fetchone()["cnt"]
                return {"success": True, "message": f"连接成功（MySQL），{profile.database} 包含 {n} 张表", "tables": n}
            finally:
                c.close()
        except pymysql.err.OperationalError as e:
            code = e.args[0] if e.args else 0
            msgs = {2013: "无法连接到服务器，请检查主机和端口", 1045: "用户名或密码错误", 1049: f"数据库 {profile.database} 不存在"}
            return {"success": False, "message": f"连接失败: {msgs.get(code, str(e))}", "tables": 0}

    def _test_dameng(self, profile: DatabaseProfile) -> dict:
        try:
            from app.database.backends.dameng_backend import _import_dmpython
            dmPython = _import_dmpython()
            c = dmPython.connect(
                server=profile.host, port=profile.port,
                user=profile.user, password=profile.password,
                login_timeout=config.DB_CONNECT_TIMEOUT,
            )
            try:
                cur = c.cursor()
                # Dameng uses Oracle-style data dictionary
                owner = profile.user.upper()
                cur.execute(
                    f"SELECT COUNT(*) AS CNT FROM ALL_TABLES WHERE OWNER = '{owner}'"
                )
                row = cur.fetchone()
                n = row[0] if row else 0
                cur.close()
                return {"success": True, "message": f"连接成功（达梦），用户 {owner} 拥有 {n} 张表", "tables": n}
            finally:
                c.close()
        except Exception as e:
            msg = str(e).replace('\n', ' ')
            if "password" in msg.lower() or "login" in msg.lower():
                return {"success": False, "message": "连接失败: 用户名或密码错误", "tables": 0}
            return {"success": False, "message": f"连接失败: {msg[:120]}", "tables": 0}

    def _test_postgres(self, profile: DatabaseProfile) -> dict:
        try:
            import psycopg2
            c = psycopg2.connect(
                host=profile.host, port=profile.port,
                user=profile.user, password=profile.password,
                dbname=profile.database, connect_timeout=config.DB_CONNECT_TIMEOUT,
            )
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS cnt FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA NOT IN ('pg_catalog', 'information_schema') "
                        "AND TABLE_TYPE = 'BASE TABLE'"
                    )
                    n = cur.fetchone()[0]
                return {"success": True, "message": f"连接成功（PostgreSQL），{profile.database} 包含 {n} 张表", "tables": n}
            finally:
                c.close()
        except Exception as e:
            msg = str(e).replace('\n', ' ')
            if "password" in msg.lower():
                return {"success": False, "message": "连接失败: 用户名或密码错误", "tables": 0}
            if "does not exist" in msg.lower():
                return {"success": False, "message": f"连接失败: 数据库 {profile.database} 不存在", "tables": 0}
            return {"success": False, "message": f"连接失败: {msg[:120]}", "tables": 0}

    # ---- Cursor & Execute ----

    @contextmanager
    def cursor(self):
        backend = get_backend(self.db_type)
        raw_cur = backend.create_cursor(self.conn)
        if self.db_type in ("postgresql",):
            cur = _PGCursorWrapper(raw_cur)
        elif self.db_type in ("dameng", "dm", "dm8"):
            cur = _TupleCursorWrapper(raw_cur)
        else:
            cur = raw_cur
        try:
            yield cur
        finally:
            cur.close()

    def execute(self, sql: str) -> dict:
        """Execute a read-only SQL query and return structured results."""
        start = time.time()
        with self.cursor() as cursor:
            cursor.execute(sql)
            if cursor.description:
                if self.db_type in ("postgresql",):
                    columns = [col.name for col in cursor.description]
                elif self.db_type in ("dameng", "dm", "dm8"):
                    columns = [col[0].upper() for col in cursor.description]
                else:
                    columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()
                row_list = [
                    [row.get(col) for col in columns]
                    for row in rows
                ]
                result = {
                    "columns": columns,
                    "rows": row_list,
                    "row_count": len(rows),
                    "execution_ms": round((time.time() - start) * 1000, 1),
                }
            else:
                result = {"columns": [], "rows": [], "row_count": 0, "execution_ms": round((time.time() - start) * 1000, 1)}
        return result

    def close(self):
        if self._conn:
            try:
                get_backend(self.db_type).close(self._conn)
            except Exception:
                pass
            self._conn = None
            self._current_profile = None
            self._backend_type = "mysql"

    # ---- Identifier quoting ----

    def quote(self, name: str) -> str:
        """Quote an identifier for the current backend."""
        return get_backend(self.db_type).quote_identifier(name)


# Singleton
db = DatabaseConnection()
