"""Database backend factory."""

from app.database.backends.base import DatabaseBackend
from app.database.backends.mysql_backend import MySQLBackend
from app.database.backends.postgres_backend import PostgresBackend
from app.database.backends.dameng_backend import DamengBackend

# Singleton instances — stateless, safe to reuse
_mysql = MySQLBackend()
_postgres = PostgresBackend()
_dameng = DamengBackend()


def get_backend(db_type: str) -> DatabaseBackend:
    """Get the backend instance for a database type.

    Args:
        db_type: 'mysql', 'mariadb', 'postgresql', 'postgres', 'pg', 'dameng', 'dm'

    Returns:
        DatabaseBackend instance

    Raises:
        ValueError: if db_type is unsupported
    """
    t = db_type.lower() if db_type else "mysql"
    if t in ("mysql", "mariadb"):
        return _mysql
    elif t in ("postgresql", "postgres", "pg"):
        return _postgres
    elif t in ("dameng", "dm", "dm8"):
        return _dameng
    else:
        raise ValueError(
            f"不支持的数据库类型: {db_type}。"
            f"当前支持: MySQL, MariaDB, PostgreSQL, Dameng(达梦)"
        )
