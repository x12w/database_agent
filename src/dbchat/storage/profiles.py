"""连接配置仓储 —— 取代 `data/profiles.json`。

历史实现每次读写整个 JSON 文件，并且把"读失败"当成"没有配置"处理
（`except (json.JSONDecodeError, KeyError): pass` 之后回落到默认库），
这会让用户以为在查 A 库、实际连的是 B 库。这里读失败一律显式报错。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence

from dbchat.domain.profile import DatabaseProfile, DialectKind, parse_dialect_kind
from dbchat.storage.db import OffloadableRepo, Storage, get_storage, utc_now_str

logger = logging.getLogger(__name__)

_COLUMNS = (
    "id, name, host, port, user, password, database, kind, description, "
    "created_at, updated_at"
)


def _row_to_profile(row: sqlite3.Row) -> DatabaseProfile:
    return DatabaseProfile(
        id=str(row["id"]),
        name=row["name"] or "",
        host=row["host"] or "127.0.0.1",
        port=int(row["port"] or 3306),
        user=row["user"] or "root",
        password=row["password"] or "",
        database=row["database"] or "",
        kind=_parse_kind(row["kind"]),
        description=row["description"] or "",
    )


def _parse_kind(raw: str | None) -> DialectKind:
    """库里存的 kind 可能是历史别名（db_type 的写法）。"""
    try:
        return parse_dialect_kind(raw)
    except ValueError as exc:
        logger.warning("连接配置里的数据库类型无法识别（%s），按 MySQL 处理: %s", raw, exc)
        return DialectKind.MYSQL


class ProfileRepository(OffloadableRepo):
    """连接配置的 CRUD。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self._db = storage or get_storage()

    def list_all(self) -> list[DatabaseProfile]:
        """全部配置，按创建时间排序。"""
        rows = self._db.query(f"SELECT {_COLUMNS} FROM profiles ORDER BY created_at, id")
        return [_row_to_profile(row) for row in rows]

    def get(self, profile_id: str) -> DatabaseProfile | None:
        """按 id 取一条。"""
        row = self._db.query_one(f"SELECT {_COLUMNS} FROM profiles WHERE id = ?", (profile_id,))
        return _row_to_profile(row) if row is not None else None

    def count(self) -> int:
        """配置条数。"""
        row = self._db.query_one("SELECT COUNT(*) AS n FROM profiles")
        return int(row["n"]) if row is not None else 0

    def upsert(self, profile: DatabaseProfile) -> None:
        """新增或覆盖一条配置。"""
        now = utc_now_str()
        self._db.execute(
            """
            INSERT INTO profiles (id, name, host, port, user, password, database,
                                  kind, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                host = excluded.host,
                port = excluded.port,
                user = excluded.user,
                password = excluded.password,
                database = excluded.database,
                kind = excluded.kind,
                description = excluded.description,
                updated_at = excluded.updated_at
            """,
            (
                profile.id,
                profile.name,
                profile.host,
                profile.port,
                profile.user,
                profile.password,
                profile.database,
                profile.kind.value,
                profile.description,
                now,
                now,
            ),
        )

    def replace_all(self, profiles: Sequence[DatabaseProfile]) -> None:
        """整体替换（删除列表之外的所有配置），在一个事务里完成。"""

        def op(conn: sqlite3.Connection) -> None:
            keep = [p.id for p in profiles]
            if keep:
                placeholders = ", ".join("?" for _ in keep)
                conn.execute(f"DELETE FROM profiles WHERE id NOT IN ({placeholders})", keep)
            else:
                conn.execute("DELETE FROM profiles")
            for profile in profiles:
                now = utc_now_str()
                conn.execute(
                    """
                    INSERT INTO profiles (id, name, host, port, user, password, database,
                                          kind, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        host = excluded.host,
                        port = excluded.port,
                        user = excluded.user,
                        password = excluded.password,
                        database = excluded.database,
                        kind = excluded.kind,
                        description = excluded.description,
                        updated_at = excluded.updated_at
                    """,
                    (
                        profile.id,
                        profile.name,
                        profile.host,
                        profile.port,
                        profile.user,
                        profile.password,
                        profile.database,
                        profile.kind.value,
                        profile.description,
                        now,
                        now,
                    ),
                )

        self._db.run(op)

    def delete(self, profile_id: str) -> bool:
        """删除一条配置；不存在返回 False。"""
        row = self._db.query_one("SELECT id FROM profiles WHERE id = ?", (profile_id,))
        if row is None:
            return False
        self._db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        return True

    def ensure_default(self, default: DatabaseProfile) -> list[DatabaseProfile]:
        """空表时写入默认配置并返回。

        语义与历史 `config.load_profiles()` 一致：没有配置就用 `.env` 的
        默认连接初始化一份。
        """
        if self.count() > 0:
            return self.list_all()
        self.upsert(default)
        return self.list_all()


_repository: ProfileRepository | None = None


def get_profile_repository(storage: Storage | None = None) -> ProfileRepository:
    """共享的仓储实例。"""
    global _repository
    if storage is not None:
        return ProfileRepository(storage)
    if _repository is None:
        _repository = ProfileRepository()
    return _repository


__all__ = ["ProfileRepository", "get_profile_repository"]
