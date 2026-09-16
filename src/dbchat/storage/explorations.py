"""探索报告仓储 —— 取代 `data/explorations/{profile_id}.json`。

一个数据库连接对应一份报告（重新探索就覆盖）。上层用它判断"这个库是否
已经探索过"（历史 `_is_explored()` 就是判断文件是否存在），所以
`is_explored` 必须与 `get` 一致：有记录才算探索过，报告为空也算。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from dbchat.storage.db import (
    OffloadableRepo,
    Storage,
    dump_json,
    get_storage,
    load_json,
    utc_now_str,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class Exploration:
    """一次数据库探索的结果。"""

    profile_id: str
    report: str = ""
    #: 待确认的编码列候选（`{table, column, samples, ...}`）。
    candidates: tuple[dict[str, Any], ...] = ()
    created_at: str = ""


def _row_to_exploration(row: sqlite3.Row) -> Exploration:
    raw = load_json(row["candidates"], [])
    candidates = tuple(item for item in raw if isinstance(item, dict))
    return Exploration(
        profile_id=str(row["profile_id"]),
        report=row["report"] or "",
        candidates=candidates,
        created_at=row["created_at"] or "",
    )


class ExplorationRepository(OffloadableRepo):
    """探索报告的读写。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self._db = storage or get_storage()

    def save(
        self,
        profile_id: str,
        report: str,
        candidates: Sequence[dict[str, Any]] | None = None,
        created_at: str | None = None,
    ) -> Exploration:
        """写入/覆盖某 profile 的探索报告。"""
        stamp = created_at or utc_now_str()
        payload = dump_json(list(candidates)) if candidates else None
        self._db.execute(
            """
            INSERT INTO explorations (profile_id, report, candidates, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                report = excluded.report,
                candidates = excluded.candidates,
                created_at = excluded.created_at
            """,
            (profile_id, report, payload, stamp),
        )
        return Exploration(
            profile_id=profile_id,
            report=report,
            candidates=tuple(dict(item) for item in (candidates or ())),
            created_at=stamp,
        )

    def get(self, profile_id: str) -> Exploration | None:
        """取某 profile 的探索报告。"""
        row = self._db.query_one(
            "SELECT profile_id, report, candidates, created_at FROM explorations "
            "WHERE profile_id = ?",
            (profile_id,),
        )
        return _row_to_exploration(row) if row is not None else None

    def report(self, profile_id: str) -> str:
        """只要报告正文；没有则返回空串（历史 `get_exploration_report` 语义）。"""
        row = self._db.query_one(
            "SELECT report FROM explorations WHERE profile_id = ?", (profile_id,)
        )
        return (row["report"] or "") if row is not None else ""

    def candidates(self, profile_id: str) -> list[dict[str, Any]]:
        """只要候选列。"""
        found = self.get(profile_id)
        return list(found.candidates) if found is not None else []

    def is_explored(self, profile_id: str) -> bool:
        """该 profile 是否已探索过。

        与历史 `_is_explored()` 等价：有记录即已探索 —— 报告为空也算，
        否则 agent 会对同一个库反复做全量探索。
        """
        row = self._db.query_one(
            "SELECT 1 AS ok FROM explorations WHERE profile_id = ?", (profile_id,)
        )
        return row is not None

    def list_all(self) -> list[Exploration]:
        """全部探索报告（最新在前）。"""
        rows = self._db.query(
            "SELECT profile_id, report, candidates, created_at FROM explorations "
            "ORDER BY created_at DESC, profile_id"
        )
        return [_row_to_exploration(row) for row in rows]

    def count(self) -> int:
        """报告数量。"""
        row = self._db.query_one("SELECT COUNT(*) AS n FROM explorations")
        return int(row["n"]) if row is not None else 0

    def delete(self, profile_id: str) -> bool:
        """删除某 profile 的报告（强制重新探索用）。"""
        if not self.is_explored(profile_id):
            return False
        self._db.execute("DELETE FROM explorations WHERE profile_id = ?", (profile_id,))
        return True


_repository: ExplorationRepository | None = None


def get_exploration_repository(storage: Storage | None = None) -> ExplorationRepository:
    """共享的仓储实例。"""
    global _repository
    if storage is not None:
        return ExplorationRepository(storage)
    if _repository is None:
        _repository = ExplorationRepository()
    return _repository


__all__ = ["Exploration", "ExplorationRepository", "get_exploration_repository"]
