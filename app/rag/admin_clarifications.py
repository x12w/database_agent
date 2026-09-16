"""Admin clarification store — persistent Q&A for ambiguous database columns.

The Analysis Agent can ask the human admin for clarification when it encounters
columns whose meaning cannot be inferred from sample data alone (e.g. encoded
values like '01'/'02'/'03' without context). Admin answers are persisted and
indexed by RAG for use in future queries.

Data is now stored per-database-profile under data/clarifications/{profile_id}.json.
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import config


@dataclass
class ClarificationEntry:
    """A single admin Q&A entry."""
    question_id: str
    table_name: str
    column_name: str        # empty string for table-level questions
    question: str
    sample_values: list = field(default_factory=list)
    answer: str = ""        # empty until answered
    status: str = "pending"  # pending | answered | ignored
    created_at: str = ""
    answered_at: str = ""
    profile_id: str = ""    # which database profile this entry belongs to

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "table_name": self.table_name,
            "column_name": self.column_name,
            "question": self.question,
            "sample_values": self.sample_values,
            "answer": self.answer,
            "status": self.status,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
            "profile_id": self.profile_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ClarificationEntry":
        return cls(
            question_id=d.get("question_id", ""),
            table_name=d.get("table_name", ""),
            column_name=d.get("column_name", ""),
            question=d.get("question", ""),
            sample_values=d.get("sample_values", []),
            answer=d.get("answer", ""),
            status=d.get("status", "pending"),
            created_at=d.get("created_at", ""),
            answered_at=d.get("answered_at", ""),
            profile_id=d.get("profile_id", ""),
        )


class ClarificationStore:
    """Persistent store for admin clarification Q&A pairs, per database profile.

    Each profile has its own JSON file at data/clarifications/{profile_id}.json.
    Entries are lazy-loaded on first access per profile.

    Singleton pattern — use `clarification_store` at module level.
    """

    def __init__(self):
        # {profile_id: {question_id: ClarificationEntry}}
        self._stores: dict[str, dict[str, ClarificationEntry]] = {}
        self._migrated = False

    # ---- Public API (all methods require profile_id) ----

    def add_question(
        self,
        profile_id: str,
        table_name: str,
        question: str,
        column_name: str = "",
        sample_values: list = None,
    ) -> str:
        """Save a new pending question. Returns the question_id."""
        self._ensure_loaded(profile_id)
        qid = uuid.uuid4().hex[:12]
        entry = ClarificationEntry(
            question_id=qid,
            table_name=table_name,
            column_name=column_name,
            question=question,
            sample_values=sample_values or [],
            status="pending",
            profile_id=profile_id,
        )
        store = self._stores[profile_id]
        store[qid] = entry
        self._save(profile_id)
        return qid

    def answer_question(self, profile_id: str, question_id: str, answer: str) -> bool:
        """Mark a question as answered. Returns False if not found or already answered."""
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        entry = store.get(question_id)
        if entry is None:
            return False
        entry.answer = answer.strip()
        entry.status = "answered"
        entry.answered_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self._save(profile_id)
        return True

    def extract_mappings(self, answer: str) -> dict[str, str]:
        """Extract key-value mappings from an admin answer.

        Recognizes patterns like:
        - '01=在职, 02=离职'
        - 'A代表优秀, B代表良好'
        - '01:在职, 02:离职'
        Returns {code: meaning} dict, or empty dict if no clear mappings found.
        """
        import re
        mappings = {}
        parts = re.split(r'[,;，；]\s*', answer)
        for part in parts:
            part = part.strip()
            match = re.match(r'([^\s=：:代表表示]+)\s*[=：:]\s*(.+)', part)
            if not match:
                match = re.match(r'([^\s]+)\s*(?:代表|表示|意为|意思是)\s*(.+)', part)
            if match:
                code = match.group(1).strip().strip("'\"\"''")
                meaning = match.group(2).strip().strip("'\"\"''")
                if code and meaning and len(code) <= 20:
                    mappings[code] = meaning
        return mappings

    def ignore_question(self, profile_id: str, question_id: str) -> bool:
        """Dismiss a question without answering."""
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        entry = store.get(question_id)
        if entry is None:
            return False
        entry.status = "ignored"
        self._save(profile_id)
        return True

    def get_pending(self, profile_id: str) -> list[ClarificationEntry]:
        """Return all unanswered questions for a profile (newest first)."""
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        pending = [e for e in store.values() if e.status == "pending"]
        pending.sort(key=lambda e: e.created_at, reverse=True)
        return pending

    def get_all_answered(self, profile_id: str) -> list[ClarificationEntry]:
        """Return all answered questions for a profile (newest first)."""
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        answered = [e for e in store.values() if e.status == "answered"]
        answered.sort(key=lambda e: e.answered_at, reverse=True)
        return answered

    def get_all(self, profile_id: str) -> list[ClarificationEntry]:
        """Return all entries for a profile (newest first), regardless of status."""
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        all_entries = list(store.values())
        all_entries.sort(key=lambda e: e.created_at, reverse=True)
        return all_entries

    def get_clarification(self, profile_id: str, table_name: str, column_name: str = "") -> list[ClarificationEntry]:
        """Direct lookup for clarifications about a specific table/column in a profile.

        Case-insensitive table name match. Returns answered entries only.
        """
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        results = []
        t_lower = table_name.lower()
        c_lower = column_name.lower() if column_name else ""
        for entry in store.values():
            if entry.status != "answered":
                continue
            if entry.table_name.lower() != t_lower:
                continue
            if c_lower and entry.column_name.lower() != c_lower:
                continue
            results.append(entry)
        return results

    def get_pending_count(self, profile_id: str) -> int:
        """Return the number of pending questions for a profile."""
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        return sum(1 for e in store.values() if e.status == "pending")

    def get_pending_summary(self, profile_id: str) -> dict[str, list[str]]:
        """Return pending questions grouped by table for schema context display.

        Returns:
            {table_name: [question_summary, ...]}
        """
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        by_table: dict[str, list[str]] = {}
        for e in store.values():
            if e.status != "pending":
                continue
            label = f"`{e.column_name}`" if e.column_name else "（整表）"
            by_table.setdefault(e.table_name, []).append(label)
        return by_table

    def get_known_columns(self, profile_id: str) -> set[tuple[str, str]]:
        """Return set of (table_name, column_name) tuples that have been clarified.

        Used to detect whether a column has already been explored (answered or pending).
        """
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        known = set()
        for e in store.values():
            if e.status in ("answered", "pending"):
                known.add((e.table_name.lower(), e.column_name.lower() if e.column_name else ""))
        return known

    def get_exploration_progress(self, profile_id: str, all_tables: dict[str, int]) -> dict:
        """Return exploration progress summary across all tables for a profile.

        Args:
            profile_id: The database profile ID.
            all_tables: {table_name: column_count} for all tables in the database.

        Returns:
            {
                "total_columns": int,
                "clarified_columns": int,  # answered
                "pending_columns": int,    # awaiting answer
                "unknown_columns": int,    # not yet explored
                "tables_with_pending": [table_name, ...],
            }
        """
        self._ensure_loaded(profile_id)
        store = self._stores.get(profile_id, {})
        total = sum(all_tables.values())
        clarified = 0
        pending = 0
        tables_with_pending = set()

        for e in store.values():
            if e.status == "answered":
                clarified += 1
            elif e.status == "pending":
                pending += 1
                tables_with_pending.add(e.table_name)

        return {
            "total_columns": total,
            "clarified_columns": clarified,
            "pending_columns": pending,
            "unknown_columns": max(0, total - clarified - pending),
            "tables_with_pending": sorted(tables_with_pending),
        }

    def build_rag_document(self, profile_id: str = None) -> str:
        """Generate a markdown document of all answered clarifications for RAG indexing.

        Args:
            profile_id: If provided, only include entries for that profile.
                        If None, include entries from ALL profiles (for chat app RAG).

        The document is structured for semantic search — each entry is a section
        that can be chunked by the RAG indexer.
        """
        if profile_id:
            self._ensure_loaded(profile_id)
            answered = self.get_all_answered(profile_id)
        else:
            # Aggregate all profiles
            answered = []
            self._load_all_profiles()
            for store in self._stores.values():
                for entry in store.values():
                    if entry.status == "answered":
                        answered.append(entry)
            answered.sort(key=lambda e: e.answered_at, reverse=True)

        if not answered:
            return ""

        lines = [
            "# 数据库管理员说明",
            "",
            "以下内容由数据库管理员提供，描述数据库表中各列的真实业务含义。",
            "这些是权威定义，优先于任何基于样本数据的推断。",
            "",
        ]

        # Group by table for readability
        by_table: dict[str, list[ClarificationEntry]] = {}
        for entry in answered:
            by_table.setdefault(entry.table_name, []).append(entry)

        for table_name, entries in sorted(by_table.items()):
            lines.append(f"## 表: `{table_name}`")
            lines.append("")
            for entry in entries:
                col_label = f"`{entry.column_name}`" if entry.column_name else "（整表）"
                lines.append(f"### {table_name}.{col_label}")
                lines.append("")

                # Extract and display mappings for easy agent consumption
                mappings = self.extract_mappings(entry.answer)
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
                    lines.append(f"*相关样本值*: {', '.join(str(v) for v in entry.sample_values)}")
                    lines.append("")
                lines.append("---")
                lines.append("")

        return "\n".join(lines)

    def get_all_profile_ids(self) -> list[str]:
        """Return all profile IDs that have clarification data."""
        self._load_all_profiles()
        return sorted(self._stores.keys())

    # ---- Persistence ----

    def _ensure_loaded(self, profile_id: str):
        """Lazy-load entries for a specific profile from disk."""
        if profile_id not in self._stores:
            self._load(profile_id)
        # Try migration on first load
        if not self._migrated:
            self._try_migrate()

    def _load_all_profiles(self):
        """Load all available profile clarification files."""
        clarifications_dir = self._clarifications_dir()
        if not clarifications_dir.exists():
            return
        for f in clarifications_dir.glob("*.json"):
            pid = f.stem
            if pid not in self._stores:
                self._load(pid)

    def _clarifications_dir(self) -> Path:
        return Path(__file__).parent.parent.parent / config.CLARIFICATIONS_PATH

    def _data_path(self, profile_id: str) -> Path:
        return self._clarifications_dir() / f"{profile_id}.json"

    def _save(self, profile_id: str):
        """Persist entries for a specific profile to its JSON file."""
        path = self._data_path(profile_id)
        os.makedirs(path.parent, exist_ok=True)
        store = self._stores.get(profile_id, {})
        data = [e.to_dict() for e in store.values()]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _load(self, profile_id: str):
        """Load entries for a specific profile from its JSON file."""
        self._stores[profile_id] = {}
        path = self._data_path(profile_id)
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            store = self._stores[profile_id]
            for d in data:
                # Ensure profile_id is set (backward compat with entries that lack it)
                if "profile_id" not in d or not d.get("profile_id"):
                    d["profile_id"] = profile_id
                entry = ClarificationEntry.from_dict(d)
                store[entry.question_id] = entry
        except (json.JSONDecodeError, KeyError):
            self._stores[profile_id] = {}

    def _try_migrate(self):
        """Migrate old single-file admin_clarifications.json to per-profile files.

        Old entries lack profile_id. We match each entry's table_name against
        schema caches of all profiles to determine which database it belongs to.
        """
        self._migrated = True
        old_path = Path(__file__).parent.parent.parent / "data" / "admin_clarifications.json"

        if not old_path.exists():
            return

        # Check if per-profile files already exist
        clarifications_dir = self._clarifications_dir()
        existing_files = list(clarifications_dir.glob("*.json")) if clarifications_dir.exists() else []
        if existing_files:
            # Already migrated — just back up the old file if not already done
            bak_path = Path(str(old_path) + ".bak")
            if not bak_path.exists():
                try:
                    os.rename(str(old_path), str(bak_path))
                    print(f"[ClarificationStore] 旧数据已备份为 {bak_path}")
                except OSError:
                    pass
            return

        # Load old data
        try:
            with open(old_path, "r", encoding="utf-8") as f:
                old_data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return

        if not old_data:
            return

        # Load all profiles to match table names
        profiles = config.load_profiles()

        # Build a mapping of table_name → [profile_ids]
        table_to_profiles: dict[str, list[str]] = {}
        for p in profiles:
            try:
                schema_file = Path(__file__).parent.parent.parent / "data" / f"schema_{p.id}.json"
                if schema_file.exists():
                    with open(schema_file, "r", encoding="utf-8") as f:
                        schema_data = json.load(f)
                    tables = schema_data.get("tables", {})
                    for tname in tables:
                        t_lower = tname.lower()
                        table_to_profiles.setdefault(t_lower, []).append(p.id)
            except Exception:
                continue

        # Assign each old entry to a profile
        assigned = 0
        unassigned = 0
        for d in old_data:
            entry = ClarificationEntry.from_dict(d)
            t_lower = entry.table_name.lower()
            profile_ids = table_to_profiles.get(t_lower, [])

            if len(profile_ids) == 1:
                entry.profile_id = profile_ids[0]
                assigned += 1
            elif len(profile_ids) > 1:
                # Table exists in multiple profiles — assign to first, warn
                entry.profile_id = profile_ids[0]
                assigned += 1
                print(f"[ClarificationStore] 警告: 表 '{entry.table_name}' 存在于多个数据库中，"
                      f"问答已分配到 {profile_ids[0]}")
            else:
                # Table not found in any profile — assign to "default"
                entry.profile_id = "default"
                unassigned += 1

            # Save to per-profile store
            if entry.profile_id not in self._stores:
                self._stores[entry.profile_id] = {}
            self._stores[entry.profile_id][entry.question_id] = entry

        # Persist each profile's entries
        os.makedirs(str(clarifications_dir), exist_ok=True)
        for pid in self._stores:
            self._save(pid)

        # Backup old file
        bak_path = Path(str(old_path) + ".bak")
        try:
            os.rename(str(old_path), str(bak_path))
        except OSError:
            pass

        print(f"[ClarificationStore] 数据迁移完成: {assigned} 条已分配, "
              f"{unassigned} 条未匹配(分配至default), "
              f"旧文件备份为 {bak_path}")


# Singleton
clarification_store = ClarificationStore()
