"""管理员问答仓储 —— 取代 `data/clarifications/{profile_id}.json`。

这一层是 `app/rag/admin_clarifications.py`（501 行）的持久化重写，**语义必须
逐条对齐**：agent 的去重逻辑（哪些列已经问过、哪些还没探索）完全依赖
`get_known_columns` / `get_clarification` / `get_exploration_progress` 的
返回值，改口径会让同一列被反复提问或者干脆漏问。

刻意保留的历史语义（看起来像 bug，但下游依赖它）：

* `get_clarification(profile_id, table, column="")` 在 `column` 为空时
  返回该表**所有列**的已答条目，而不是"整表级"条目；
* `answer_question` 对已答问题再次调用会覆盖答案并返回 True（允许修正）；
* `extract_mappings` 的识别范围就是那两条正则，多识别/少识别都会改变
  RAG 文档里"编码映射表"的内容。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
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

STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_IGNORED = "ignored"

#: 问答按逗号/分号切分（中英文标点都算）。
_MAPPING_SPLIT = re.compile(r"[,;，；]\s*")
#: `01=在职` / `01:在职` 形态。字符类里的"代表表示"是逐个字符排除，
#: 避免把"表示"误当成键的一部分。
_MAPPING_EQUALS = re.compile(r"([^\s=：:代表表示]+)\s*[=：:]\s*(.+)")
#: `A代表优秀` / `表示` / `意为` / `意思是` 形态。
_MAPPING_WORDS = re.compile(r"([^\s]+)\s*(?:代表|表示|意为|意思是)\s*(.+)")
#: 键长度上限 —— 超过这个长度的"键"几乎一定是把整句话误判成了映射。
_MAPPING_MAX_CODE_LEN = 20

_COLUMNS = (
    "question_id, profile_id, table_name, column_name, question, agent_guess, "
    "answer, status, samples, context, created_at, answered_at"
)


def extract_mappings(answer: str) -> dict[str, str]:
    """从管理员回答里抽出 `编码 → 含义` 映射。

    识别 `01=在职, 02=离职`、`A代表优秀`、`TECH:技术类` 这类写法；
    抽不出映射时返回空 dict。**正则原样移植自历史实现**，不要顺手"改进"。
    """
    mappings: dict[str, str] = {}
    for part in _MAPPING_SPLIT.split(answer):
        part = part.strip()
        match = _MAPPING_EQUALS.match(part)
        if not match:
            match = _MAPPING_WORDS.match(part)
        if not match:
            continue
        code = match.group(1).strip().strip("'\"")
        meaning = match.group(2).strip().strip("'\"")
        if code and meaning and len(code) <= _MAPPING_MAX_CODE_LEN:
            mappings[code] = meaning
    return mappings


@dataclass(slots=True)
class Clarification:
    """一条管理员问答。"""

    question_id: str
    profile_id: str = ""
    table_name: str = ""
    column_name: str = ""
    question: str = ""
    #: agent 提问时给出的猜测（管理员可以确认或纠正）。
    agent_guess: str = ""
    answer: str = ""
    status: str = STATUS_PENDING
    #: 抽样值，帮助管理员判断这一列是什么。
    sample_values: list[Any] = field(default_factory=list)
    #: 其它上下文（列类型、命中原因等）。
    context: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    answered_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = utc_now_str()

    @property
    def answered(self) -> bool:
        return self.status == STATUS_ANSWERED

    @property
    def key(self) -> tuple[str, str]:
        """该条目描述的列（表名小写, 列名小写）。"""
        return (self.table_name.lower(), self.column_name.lower() if self.column_name else "")

    def to_dict(self) -> dict[str, Any]:
        """兼容历史 JSON 的字段名（`sample_values`）。"""
        return {
            "question_id": self.question_id,
            "profile_id": self.profile_id,
            "table_name": self.table_name,
            "column_name": self.column_name,
            "question": self.question,
            "agent_guess": self.agent_guess,
            "sample_values": self.sample_values,
            "context": self.context,
            "answer": self.answer,
            "status": self.status,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, profile_id: str = "") -> Clarification:
        """从历史 JSON 还原。

        `profile_id` 参数用于补齐老数据里缺失的归属（历史实现同样在加载时
        用文件名回填）。
        """
        return cls(
            question_id=str(data.get("question_id", "")),
            profile_id=str(data.get("profile_id") or profile_id),
            table_name=data.get("table_name", ""),
            column_name=data.get("column_name", ""),
            question=data.get("question", ""),
            agent_guess=data.get("agent_guess", ""),
            answer=data.get("answer", ""),
            status=data.get("status", STATUS_PENDING) or STATUS_PENDING,
            sample_values=list(data.get("sample_values") or []),
            context=dict(data.get("context") or {}),
            created_at=data.get("created_at", ""),
            answered_at=data.get("answered_at", ""),
        )


def _row_to_clarification(row: sqlite3.Row) -> Clarification:
    return Clarification(
        question_id=str(row["question_id"]),
        profile_id=str(row["profile_id"]),
        table_name=row["table_name"] or "",
        column_name=row["column_name"] or "",
        question=row["question"] or "",
        agent_guess=row["agent_guess"] or "",
        answer=row["answer"] or "",
        status=row["status"] or STATUS_PENDING,
        sample_values=load_json(row["samples"], []),
        context=load_json(row["context"], {}),
        created_at=row["created_at"] or "",
        answered_at=row["answered_at"] or "",
    )


class ClarificationRepository(OffloadableRepo):
    """管理员问答的读写与聚合。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self._db = storage or get_storage()

    # ---- 写入 ----

    def add_question(
        self,
        profile_id: str,
        table_name: str,
        question: str,
        column_name: str = "",
        sample_values: Sequence[Any] | None = None,
        agent_guess: str = "",
        context: dict[str, Any] | None = None,
    ) -> str:
        """登记一个待确认问题，返回 question_id。"""
        question_id = uuid.uuid4().hex[:12]
        entry = Clarification(
            question_id=question_id,
            profile_id=profile_id,
            table_name=table_name,
            column_name=column_name,
            question=question,
            agent_guess=agent_guess,
            sample_values=list(sample_values or []),
            context=dict(context or {}),
            status=STATUS_PENDING,
        )
        self.upsert(entry)
        return question_id

    def upsert(self, entry: Clarification) -> None:
        """写入/覆盖一条问答（导入与手工修复用）。"""
        if not entry.created_at:
            entry.created_at = utc_now_str()
        self._db.execute(
            """
            INSERT INTO clarifications (
                question_id, profile_id, table_name, column_name, question,
                agent_guess, answer, status, samples, context, created_at, answered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id, question_id) DO UPDATE SET
                table_name = excluded.table_name,
                column_name = excluded.column_name,
                question = excluded.question,
                agent_guess = excluded.agent_guess,
                answer = excluded.answer,
                status = excluded.status,
                samples = excluded.samples,
                context = excluded.context,
                created_at = excluded.created_at,
                answered_at = excluded.answered_at
            """,
            (
                entry.question_id,
                entry.profile_id,
                entry.table_name,
                entry.column_name,
                entry.question,
                entry.agent_guess,
                entry.answer,
                entry.status,
                dump_json(entry.sample_values),
                dump_json(entry.context) if entry.context else None,
                entry.created_at,
                entry.answered_at,
            ),
        )

    def answer_question(self, profile_id: str, question_id: str, answer: str) -> bool:
        """登记管理员回答。问题不存在返回 False；重复回答会覆盖（允许修正）。"""
        if self.get(profile_id, question_id) is None:
            return False
        self._db.execute(
            """
            UPDATE clarifications
            SET answer = ?, status = ?, answered_at = ?
            WHERE profile_id = ? AND question_id = ?
            """,
            (answer.strip(), STATUS_ANSWERED, utc_now_str(), profile_id, question_id),
        )
        return True

    def ignore_question(self, profile_id: str, question_id: str) -> bool:
        """忽略一个问题（不回答）。"""
        if self.get(profile_id, question_id) is None:
            return False
        self._db.execute(
            "UPDATE clarifications SET status = ? WHERE profile_id = ? AND question_id = ?",
            (STATUS_IGNORED, profile_id, question_id),
        )
        return True

    def delete(self, profile_id: str, question_id: str) -> bool:
        """删除一条问答。"""
        if self.get(profile_id, question_id) is None:
            return False
        self._db.execute(
            "DELETE FROM clarifications WHERE profile_id = ? AND question_id = ?",
            (profile_id, question_id),
        )
        return True

    # ---- 读取 ----

    def get(self, profile_id: str, question_id: str) -> Clarification | None:
        """按 id 取一条。"""
        row = self._db.query_one(
            f"SELECT {_COLUMNS} FROM clarifications WHERE profile_id = ? AND question_id = ?",
            (profile_id, question_id),
        )
        return _row_to_clarification(row) if row is not None else None

    def get_pending(self, profile_id: str) -> list[Clarification]:
        """待确认问题（最新在前）。"""
        return self._list(profile_id, "status = ?", (STATUS_PENDING,), "created_at DESC")

    def get_all_answered(self, profile_id: str) -> list[Clarification]:
        """已回答问题（最新在前）。"""
        return self._list(profile_id, "status = ?", (STATUS_ANSWERED,), "answered_at DESC")

    def get_all(self, profile_id: str) -> list[Clarification]:
        """全部条目（最新在前）。"""
        return self._list(profile_id, None, (), "created_at DESC")

    def get_clarification(
        self, profile_id: str, table_name: str, column_name: str = ""
    ) -> list[Clarification]:
        """精确查询某表/列的管理员说明（表名大小写不敏感，仅返回已答条目）。

        `column_name` 为空时返回该表所有列的说明 —— 历史行为如此，调用方
        （提示词组装）依赖它一次拿到整表的定义。
        """
        sql = (
            f"SELECT {_COLUMNS} FROM clarifications "
            "WHERE profile_id = ? AND status = ? AND LOWER(table_name) = LOWER(?)"
        )
        params: list[Any] = [profile_id, STATUS_ANSWERED, table_name]
        if column_name:
            sql += " AND LOWER(column_name) = LOWER(?)"
            params.append(column_name)
        sql += " ORDER BY answered_at DESC, created_at DESC, rowid"
        return [_row_to_clarification(row) for row in self._db.query(sql, params)]

    def get_pending_count(self, profile_id: str) -> int:
        """待确认数量。"""
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM clarifications WHERE profile_id = ? AND status = ?",
            (profile_id, STATUS_PENDING),
        )
        return int(row["n"]) if row is not None else 0

    def get_pending_summary(self, profile_id: str) -> dict[str, list[str]]:
        """待确认问题按表分组：`{表名: [列标签, ...]}`。"""
        by_table: dict[str, list[str]] = {}
        for entry in self.get_pending(profile_id):
            label = f"`{entry.column_name}`" if entry.column_name else "（整表）"
            by_table.setdefault(entry.table_name, []).append(label)
        return by_table

    def get_known_columns(self, profile_id: str) -> set[tuple[str, str]]:
        """已经问过（待答或已答）的 (表名, 列名) 小写集合 —— agent 去重靠它。"""
        rows = self._db.query(
            """
            SELECT DISTINCT LOWER(table_name) AS t, LOWER(COALESCE(column_name, '')) AS c
            FROM clarifications
            WHERE profile_id = ? AND status IN (?, ?)
            """,
            (profile_id, STATUS_ANSWERED, STATUS_PENDING),
        )
        return {(row["t"], row["c"]) for row in rows}

    def get_exploration_progress(
        self, profile_id: str, all_tables: dict[str, int]
    ) -> dict[str, Any]:
        """探索进度汇总。

        Args:
            all_tables: `{表名: 列数}`，来自 schema。

        Returns:
            `total_columns` / `clarified_columns` / `pending_columns` /
            `unknown_columns` / `tables_with_pending`。
        """
        rows = self._db.query(
            """
            SELECT status, table_name, COUNT(*) AS n
            FROM clarifications
            WHERE profile_id = ?
            GROUP BY status, table_name
            """,
            (profile_id,),
        )
        total = sum(all_tables.values())
        clarified = 0
        pending = 0
        tables_with_pending: set[str] = set()
        for row in rows:
            count = int(row["n"])
            if row["status"] == STATUS_ANSWERED:
                clarified += count
            elif row["status"] == STATUS_PENDING:
                pending += count
                tables_with_pending.add(str(row["table_name"]))

        return {
            "total_columns": total,
            "clarified_columns": clarified,
            "pending_columns": pending,
            "unknown_columns": max(0, total - clarified - pending),
            "tables_with_pending": sorted(tables_with_pending),
        }

    def get_all_profile_ids(self) -> list[str]:
        """有问答数据的 profile。"""
        rows = self._db.query("SELECT DISTINCT profile_id FROM clarifications ORDER BY profile_id")
        return [str(row["profile_id"]) for row in rows]

    def count(self, profile_id: str | None = None) -> int:
        """条目总数（可按 profile 过滤）。"""
        if profile_id:
            row = self._db.query_one(
                "SELECT COUNT(*) AS n FROM clarifications WHERE profile_id = ?", (profile_id,)
            )
        else:
            row = self._db.query_one("SELECT COUNT(*) AS n FROM clarifications")
        return int(row["n"]) if row is not None else 0

    def _list(
        self,
        profile_id: str,
        where: str | None,
        params: Sequence[Any],
        order_by: str,
    ) -> list[Clarification]:
        sql = f"SELECT {_COLUMNS} FROM clarifications WHERE profile_id = ?"
        all_params: list[Any] = [profile_id, *params]
        if where:
            sql += f" AND {where}"
        # 时间戳精度只到秒，同秒条目用 rowid（写入顺序）兜底 —— 与历史上
        # 稳定排序 + dict 插入顺序的结果一致，且跨进程可复现。
        sql += f" ORDER BY {order_by}, rowid"
        return [_row_to_clarification(row) for row in self._db.query(sql, all_params)]

    # ---- RAG 语料 ----

    def build_rag_document(self, profile_id: str | None = None) -> str:
        """把已答问答渲染成 RAG 用的 markdown 文档。

        Args:
            profile_id: 只包含该 profile；None 表示聚合全部 profile。

        结构必须是"每个条目一个 `###` 小节"，RAG 切块依赖这个层级。
        """
        if profile_id:
            answered = self.get_all_answered(profile_id)
        else:
            answered = self._all_answered_across_profiles()

        if not answered:
            return ""

        lines = [
            "# 数据库管理员说明",
            "",
            "以下内容由数据库管理员提供，描述数据库表中各列的真实业务含义。",
            "这些是权威定义，优先于任何基于样本数据的推断。",
            "",
        ]

        by_table: dict[str, list[Clarification]] = {}
        for entry in answered:
            by_table.setdefault(entry.table_name, []).append(entry)

        for table_name, entries in sorted(by_table.items()):
            lines.append(f"## 表: `{table_name}`")
            lines.append("")
            for entry in entries:
                col_label = f"`{entry.column_name}`" if entry.column_name else "（整表）"
                lines.append(f"### {table_name}.{col_label}")
                lines.append("")

                mappings = extract_mappings(entry.answer)
                if mappings:
                    lines.append("**编码映射表**（用于 SQL 查询）:")
                    lines.append("| 编码 | 含义 |")
                    lines.append("|------|------|")
                    for code, meaning in mappings.items():
                        lines.append(f"| `{code}` | {meaning} |")
                    lines.append("")

                lines.append(f"**管理员回答**: {entry.answer}")
                lines.append("")
                if entry.sample_values:
                    joined = ", ".join(str(value) for value in entry.sample_values)
                    lines.append(f"*相关样本值*: {joined}")
                    lines.append("")
                lines.append("---")
                lines.append("")

        return "\n".join(lines)

    def _all_answered_across_profiles(self) -> list[Clarification]:
        rows = self._db.query(
            f"SELECT {_COLUMNS} FROM clarifications WHERE status = ? "
            "ORDER BY answered_at DESC, created_at DESC, rowid",
            (STATUS_ANSWERED,),
        )
        return [_row_to_clarification(row) for row in rows]

    # ---- 纯函数 ----

    @staticmethod
    def extract_mappings(answer: str) -> dict[str, str]:
        """见模块级 `extract_mappings`（保留方法形态以兼容历史调用点）。"""
        return extract_mappings(answer)


_repository: ClarificationRepository | None = None


def get_clarification_repository(storage: Storage | None = None) -> ClarificationRepository:
    """共享的仓储实例。"""
    global _repository
    if storage is not None:
        return ClarificationRepository(storage)
    if _repository is None:
        _repository = ClarificationRepository()
    return _repository


__all__ = [
    "STATUS_ANSWERED",
    "STATUS_IGNORED",
    "STATUS_PENDING",
    "Clarification",
    "ClarificationRepository",
    "extract_mappings",
    "get_clarification_repository",
]
