"""MySQL/MariaDB backend using PyMySQL."""

import pymysql
import pymysql.cursors
from app.database.backends.base import DatabaseBackend


class MySQLBackend(DatabaseBackend):
    """MySQL database backend."""

    def connect(self, host, port, user, password, database, **kwargs):
        timeout = kwargs.get("connect_timeout", 10)
        return pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset="utf8mb4",
            connect_timeout=timeout,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    def ping(self, conn) -> bool:
        try:
            conn.ping(reconnect=True)
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
        return f"`{name}`"

    def placeholder(self) -> str:
        return "%s"
