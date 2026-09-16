"""会话与消息仓储 —— 取代 `app/main.py` 里的内存 `sessions` dict。

历史实现有两个硬伤：

* 历史被砍到最近 20 条（`history[-20:]`），用户第 21 轮之后就"失忆"；
* 会话数超过 100 时按**历史长度最短**淘汰，正在进行的会话可能被删掉，
  而且刷新页面就全丢。

现在会话是持久对象：消息顺序由 `seq` 保证，列表可列举、可重开，
消息条数不设上限。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from dbchat.storage.db import OffloadableRepo, Storage, get_storage, utc_now_str

logger = logging.getLogger(__name__)

#: 自动生成标题时取的字符数。
TITLE_MAX_CHARS = 40

#: 会话内的合法角色。
VALID_ROLES = ("user", "assistant", "tool", "system")


@dataclass(slots=True, frozen=True)
class Conversation:
    """一个会话。"""

    id: str
    profile_id: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    #: None 表示不限制；空元组表示"不允许任何表"（罕见但合法）。
    table_filter: tuple[str, ...] | None = None
    message_count: int = 0


@dataclass(slots=True, frozen=True)
class StoredMessage:
    """一条持久化消息。"""

    id: int
    conversation_id: str
    role: str
    content: str
    created_at: str
    seq: int

    def to_history_item(self) -> dict[str, str]:
        """渲染成 LLM 需要的 `{"role", "content"}` 形态。"""
        return {"role": self.role, "content": self.content}


@dataclass(slots=True)
class ConversationDetail:
    """会话 + 完整消息列表。"""

    conversation: Conversation
    messages: list[StoredMessage] = field(default_factory=list)


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    raw_filter = row["table_filter"]
    table_filter: tuple[str, ...] | None = None
    if raw_filter:
        try:
            parsed = json.loads(raw_filter)
        except json.JSONDecodeError:
            logger.warning("会话 %s 的表过滤条件损坏，按「不限」处理", row["id"])
            parsed = None
        if isinstance(parsed, list):
            table_filter = tuple(str(item) for item in parsed)
    keys = row.keys()
    return Conversation(
        id=str(row["id"]),
        profile_id=str(row["profile_id"]),
        title=row["title"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        table_filter=table_filter,
        message_count=int(row["message_count"]) if "message_count" in keys else 0,
    )


def _message_from_row(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=int(row["id"]),
        conversation_id=str(row["conversation_id"]),
        role=str(row["role"]),
        content=row["content"] or "",
        created_at=row["created_at"],
        seq=int(row["seq"]),
    )


def _auto_title(content: str) -> str:
    """用首条用户消息生成标题。"""
    first_line = content.strip().splitlines()[0] if content.strip() else ""
    if len(first_line) > TITLE_MAX_CHARS:
        return first_line[:TITLE_MAX_CHARS] + "…"
    return first_line


class ConversationRepository(OffloadableRepo):
    """会话 CRUD 与消息追加。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self._db = storage or get_storage()

    # ---- 会话 ----

    def create(
        self,
        profile_id: str,
        title: str = "",
        table_filter: Sequence[str] | None = None,
    ) -> Conversation:
        """新建会话。"""
        now = utc_now_str()
        conversation_id = uuid.uuid4().hex[:12]
        payload = json.dumps(list(table_filter), ensure_ascii=False) if table_filter else None
        self._db.execute(
            """
            INSERT INTO conversations (id, profile_id, title, created_at, updated_at,
                                       table_filter)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, profile_id, title, now, now, payload),
        )
        return Conversation(
            id=conversation_id,
            profile_id=profile_id,
            title=title,
            created_at=now,
            updated_at=now,
            table_filter=tuple(table_filter) if table_filter else None,
        )

    def get(self, conversation_id: str) -> Conversation | None:
        """按 id 取会话（含消息条数）。"""
        row = self._db.query_one(
            """
            SELECT c.id, c.profile_id, c.title, c.created_at, c.updated_at,
                   c.table_filter,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                       AS message_count
            FROM conversations c
            WHERE c.id = ?
            """,
            (conversation_id,),
        )
        return _conversation_from_row(row) if row is not None else None

    def list_all(
        self, profile_id: str | None = None, limit: int | None = None
    ) -> list[Conversation]:
        """列举会话（最近更新的在前）。`profile_id` 为空表示全部数据库。"""
        sql = """
            SELECT c.id, c.profile_id, c.title, c.created_at, c.updated_at,
                   c.table_filter,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id)
                       AS message_count
            FROM conversations c
        """
        params: list[object] = []
        if profile_id:
            sql += " WHERE c.profile_id = ?"
            params.append(profile_id)
        sql += " ORDER BY c.updated_at DESC, c.id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [_conversation_from_row(row) for row in self._db.query(sql, params)]

    def latest(self, profile_id: str) -> Conversation | None:
        """最近更新的会话 —— 用于"重新打开上次的对话"。"""
        found = self.list_all(profile_id, limit=1)
        return found[0] if found else None

    def rename(self, conversation_id: str, title: str) -> bool:
        """改标题。"""
        if self._db.query_one("SELECT id FROM conversations WHERE id = ?", (conversation_id,)) is None:
            return False
        self._db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, utc_now_str(), conversation_id),
        )
        return True

    def set_table_filter(self, conversation_id: str, table_filter: Sequence[str] | None) -> bool:
        """设置/清除表过滤条件。"""
        if self._db.query_one("SELECT id FROM conversations WHERE id = ?", (conversation_id,)) is None:
            return False
        payload = json.dumps(list(table_filter), ensure_ascii=False) if table_filter else None
        self._db.execute(
            "UPDATE conversations SET table_filter = ?, updated_at = ? WHERE id = ?",
            (payload, utc_now_str(), conversation_id),
        )
        return True

    def touch(self, conversation_id: str) -> None:
        """刷新 updated_at。"""
        self._db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (utc_now_str(), conversation_id),
        )

    def delete(self, conversation_id: str) -> bool:
        """删除会话及其消息（外键级联）。"""
        if self._db.query_one("SELECT id FROM conversations WHERE id = ?", (conversation_id,)) is None:
            return False
        self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return True

    def count(self, profile_id: str | None = None) -> int:
        """会话数量。"""
        if profile_id:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM conversations WHERE profile_id = ?", (profile_id,)
            )
        else:
            row = self._db.query_one("SELECT COUNT(*) AS n FROM conversations")
        return int(row["n"]) if row is not None else 0

    # ---- 消息 ----

    def store_message(self, conversation_id: str, role: str, content: str) -> StoredMessage:
        """追加一条消息。

        会话不存在时外键会直接报错 —— 静默写进"孤儿"会话比报错更糟。
        """

        def op(conn: sqlite3.Connection) -> StoredMessage:
            now = utc_now_str()
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            seq = int(row["m"]) + 1
            cursor = conn.execute(
                """
                INSERT INTO messages (conversation_id, role, content, created_at, seq)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, role, content, now, seq),
            )
            # 首条用户消息顺带给会话起个标题
            if role == "user":
                conv = conn.execute(
                    "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
                if conv is not None and not (conv["title"] or "").strip():
                    conn.execute(
                        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                        (_auto_title(content), now, conversation_id),
                    )
                else:
                    conn.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (now, conversation_id),
                    )
            else:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
                )
            return StoredMessage(
                id=int(cursor.lastrowid or 0),
                conversation_id=conversation_id,
                role=role,
                content=content,
                created_at=now,
                seq=seq,
            )

        return self._db.run(op)

    def messages(self, conversation_id: str, limit: int | None = None) -> list[StoredMessage]:
        """会话的全部消息（按 seq 升序）。

        Args:
            limit: 只取**最近** n 条（仍按时间升序返回）。不传表示全部 ——
                历史实现在这里硬编码了 20，导致长对话被截断。
        """
        if limit is None:
            rows = self._db.query(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq",
                (conversation_id,),
            )
            return [_message_from_row(row) for row in rows]

        rows = self._db.query(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?",
            (conversation_id, int(limit)),
        )
        return [_message_from_row(row) for row in reversed(rows)]

    def message_count(self, conversation_id: str) -> int:
        """消息条数。"""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?", (conversation_id,)
        )
        return int(row["n"]) if row is not None else 0

    def history(self, conversation_id: str, limit: int | None = None) -> list[dict[str, str]]:
        """LLM 需要的对话历史 `[{"role", "content"}, ...]`。"""
        return [m.to_history_item() for m in self.messages(conversation_id, limit)]

    def detail(self, conversation_id: str, limit: int | None = None) -> ConversationDetail | None:
        """会话 + 消息，一次取回（重开对话用）。"""
        conversation = self.get(conversation_id)
        if conversation is None:
            return None
        return ConversationDetail(
            conversation=conversation, messages=self.messages(conversation_id, limit)
        )


_repository: ConversationRepository | None = None


def get_conversation_repository(storage: Storage | None = None) -> ConversationRepository:
    """共享的仓储实例。"""
    global _repository
    if storage is not None:
        return ConversationRepository(storage)
    if _repository is None:
        _repository = ConversationRepository()
    return _repository


__all__ = [
    "TITLE_MAX_CHARS",
    "VALID_ROLES",
    "Conversation",
    "ConversationDetail",
    "ConversationRepository",
    "StoredMessage",
    "get_conversation_repository",
]
