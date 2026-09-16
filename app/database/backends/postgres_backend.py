"""PostgreSQL backend using psycopg2."""

import psycopg2
import psycopg2.extras
from app.database.backends.base import DatabaseBackend


class PostgresBackend(DatabaseBackend):
    """PostgreSQL database backend."""

    def connect(self, host, port, user, password, database, **kwargs):
        timeout = kwargs.get("connect_timeout", 10)
        conn = psycopg2.connect(
            host=host, port=port, user=user, password=password,
            dbname=database, connect_timeout=timeout,
        )
        conn.autocommit = True
        return conn

    def ping(self, conn) -> bool:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
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
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def quote_identifier(self, name: str) -> str:
        return f'"{name}"'

    def placeholder(self) -> str:
        return "%s"
