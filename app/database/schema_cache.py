"""Schema metadata cache with freshness checks — generic, no hardcoded table names.

Supports multiple database profiles — each profile gets its own schema cache.
"""

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from app.config import config
from app.database.connection import db

# Global active table filter — the single source of truth.
# Set by process_message() before any schema operations.
# build_schema_snapshot() checks this automatically.
_active_table_filter: list = None


def set_active_table_filter(tables: list = None):
    """Set the global table filter. Affects all subsequent schema operations."""
    global _active_table_filter
    _active_table_filter = tables
    print(f"[SchemaCache] Table filter set: {tables if tables else 'ALL'}")


def get_active_table_filter() -> list:
    """Get the current global table filter."""
    return _active_table_filter


@dataclass
class ColumnMeta:
    name: str
    data_type: str
    column_type: str
    nullable: bool
    key: str
    default: str = ""
    comment: str = ""


@dataclass
class TableMeta:
    name: str
    comment: str = ""
    columns: list = field(default_factory=list)
    row_count: int = 0
    indexes: list = field(default_factory=list)
    foreign_keys: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    sample_rows: list = field(default_factory=list)   # first 3 rows as samples


@dataclass
class ForeignKeyMeta:
    constraint_name: str
    table_name: str
    column_name: str
    referenced_table: str
    referenced_column: str
    delete_rule: str = ""
    update_rule: str = ""


@dataclass
class GenericSummary:
    """Generic database summary — no hardcoded table/column names."""
    total_tables: int = 0
    total_rows_estimate: int = 0
    # {table_name: {row_count, columns: [...], sample_values: {col: [...]}}}
    table_summaries: dict = field(default_factory=dict)
    # {table_name: {col_name: {null_ratio, value_type, sample_vals, description}}}
    column_profiles: dict = field(default_factory=dict)
    foreign_key_count: int = 0


@dataclass
class SchemaSnapshot:
    captured_at: str = ""
    db_name: str = ""
    tables: dict = field(default_factory=dict)         # {name: TableMeta}
    foreign_keys: list = field(default_factory=list)   # [ForeignKeyMeta]
    checksums: dict = field(default_factory=dict)
    summary: Optional[GenericSummary] = None


class SchemaCache:
    """Caches database schema metadata for a single database profile."""

    def __init__(self, cache_file: str = None):
        self._snapshot: Optional[SchemaSnapshot] = None
        self._last_refresh: float = 0
        self._cache_file = cache_file
        self._lock = threading.Lock()
        self._active_filter: list = None  # Currently applied table filter

    @property
    def snapshot(self) -> Optional[SchemaSnapshot]:
        return self._snapshot

    def is_fresh(self) -> bool:
        if self._snapshot is None:
            return False
        elapsed = time.time() - self._last_refresh
        return elapsed < config.CACHE_TTL_SECONDS

    def ensure_fresh(self, table_filter: list = None) -> bool:
        # Detect filter changes — force rebuild
        filter_changed = (table_filter != self._active_filter)
        if filter_changed:
            self._active_filter = table_filter
            return self._refresh(table_filter)
        if self.is_fresh() and table_filter is None:
            return True
        if self._snapshot is None:
            return self._refresh(table_filter)
        try:
            current_checksum = self._compute_checksums(self._snapshot.db_name)
            if current_checksum == self._snapshot.checksums:
                self._last_refresh = time.time()
                return True
        except Exception:
            pass
        return self._refresh(table_filter)

    def _refresh(self, table_filter: list = None) -> bool:
        try:
            db_name = db.current_db_name
            # Fast mode: skip per-table sample/distribution queries
            # These are loaded lazily on-demand via ensure_table_samples()
            self._snapshot = build_schema_snapshot(db_name, fast_mode=True,
                                                    table_filter=table_filter)
            self._last_refresh = time.time()
            self._persist()
            return True
        except Exception as e:
            import traceback
            print(f"[SchemaCache] Refresh failed for {db.current_db_name}: {e}")
            traceback.print_exc()
            return self._snapshot is not None

    def _compute_checksums(self, db_name: str) -> dict:
        is_dm = (db.db_type in ("dameng", "dm", "dm8"))
        is_pg = (db.db_type == "postgresql")
        with db.cursor() as cursor:
            if is_dm:
                cursor.execute(
                    "SELECT TABLE_NAME, CAST(NUM_ROWS AS VARCHAR) AS ts "
                    "FROM ALL_TABLES WHERE OWNER = USER"
                )
            elif is_pg:
                cursor.execute(
                    "SELECT tablename AS TABLE_NAME, "
                    "pg_catalog.pg_relation_size(pc.oid)::TEXT AS ts "
                    "FROM pg_catalog.pg_tables t "
                    "JOIN pg_catalog.pg_class pc ON pc.relname = t.tablename "
                    "AND pc.relnamespace = (SELECT oid FROM pg_catalog.pg_namespace "
                    "WHERE nspname = t.schemaname) "
                    "WHERE t.schemaname NOT IN ('pg_catalog', 'information_schema')"
                )
            else:
                cursor.execute(
                    "SELECT TABLE_NAME, COALESCE(UPDATE_TIME, CREATE_TIME) AS ts "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s",
                    (db_name,),
                )
            rows = cursor.fetchall()
        raw = "|".join(f"{r['TABLE_NAME']}={r['ts']}" for r in sorted(rows, key=lambda r: r['TABLE_NAME']))
        return {"info_schema_md5": hashlib.md5(raw.encode()).hexdigest()}

    def _persist(self):
        """Persist the current snapshot to disk (internal, caller must hold lock if needed)."""
        if self._snapshot is None or not self._cache_file:
            return
        try:
            os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
            data = {
                "captured_at": self._snapshot.captured_at,
                "db_name": self._snapshot.db_name,
                "tables": {
                    name: {
                        "name": t.name,
                        "comment": t.comment,
                        "row_count": t.row_count,
                        "columns": [asdict(c) for c in t.columns],
                        "indexes": t.indexes,
                        "foreign_keys": t.foreign_keys,
                        "checks": t.checks,
                        "sample_rows": t.sample_rows,
                    }
                    for name, t in self._snapshot.tables.items()
                },
                "foreign_keys": [asdict(fk) for fk in self._snapshot.foreign_keys],
                "checksums": self._snapshot.checksums,
                "summary": {
                    "total_tables": self._snapshot.summary.total_tables,
                    "total_rows_estimate": self._snapshot.summary.total_rows_estimate,
                    "table_summaries": self._snapshot.summary.table_summaries,
                    "column_profiles": self._snapshot.summary.column_profiles,
                    "foreign_key_count": self._snapshot.summary.foreign_key_count,
                } if self._snapshot.summary else None,
            }
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            pass

    def persist(self):
        """Thread-safe public wrapper for persisting the current snapshot."""
        with self._lock:
            self._persist()

    def load_from_disk(self) -> bool:
        if not self._cache_file or not os.path.exists(self._cache_file):
            return False
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._snapshot = SchemaSnapshot(
                captured_at=data.get("captured_at", ""),
                db_name=data.get("db_name", ""),
                tables={
                    name: TableMeta(
                        name=t["name"],
                        comment=t.get("comment", ""),
                        row_count=t.get("row_count", 0),
                        columns=[ColumnMeta(**c) for c in t.get("columns", [])],
                        indexes=t.get("indexes", []),
                        foreign_keys=t.get("foreign_keys", []),
                        checks=t.get("checks", []),
                        sample_rows=t.get("sample_rows", []),
                    )
                    for name, t in data.get("tables", {}).items()
                },
                foreign_keys=[ForeignKeyMeta(**fk) for fk in data.get("foreign_keys", [])],
                checksums=data.get("checksums", {}),
                summary=GenericSummary(**data["summary"]) if data.get("summary") else None,
            )
            self._last_refresh = time.time()
            # Merge samples from detailed cache if available (background explorer progress)
            self._merge_detailed_cache()
            return True
        except Exception:
            return False

    def _merge_detailed_cache(self):
        """Merge sample data from detailed cache (background explorer) into the snapshot."""
        if not self._cache_file or not self._snapshot:
            return
        # Derive detailed cache path from main cache path
        # schema_{id}.json → detailed_cache/{id}.json
        import re
        match = re.search(r'schema_(.+)\.json', os.path.basename(self._cache_file))
        if not match:
            return
        profile_id = match.group(1)
        detailed_path = os.path.join("data/detailed_cache", f"{profile_id}.json")
        if not os.path.exists(detailed_path):
            return
        try:
            with open(detailed_path, "r", encoding="utf-8") as f:
                detailed = json.load(f)
            table_summaries = detailed.get("table_summaries", {})
            merged = 0
            for tname, info in table_summaries.items():
                if tname in self._snapshot.tables:
                    meta = self._snapshot.tables[tname]
                    # Only merge if main cache doesn't already have samples
                    if not meta.sample_rows and info.get("sample_rows"):
                        meta.sample_rows = info["sample_rows"]
                        merged += 1
            if merged > 0:
                # Persist the merged result
                self._persist()
                print(f"[SchemaCache] Merged samples for {merged} tables from detailed cache")
        except Exception as e:
            print(f"[SchemaCache] Failed to merge detailed cache: {e}")


class SchemaCacheManager:
    """Manages schema caches for multiple database profiles."""

    def __init__(self):
        self._caches: dict[str, SchemaCache] = {}  # profile_id → SchemaCache

    def get_cache(self, profile_id: str) -> SchemaCache:
        """Get or create a schema cache for a profile."""
        if profile_id not in self._caches:
            cache_file = f"data/schema_{profile_id}.json"
            cache = SchemaCache(cache_file=cache_file)
            cache.load_from_disk()
            self._caches[profile_id] = cache
        return self._caches[profile_id]

    def ensure_fresh(self, profile_id: str, table_filter: list = None) -> SchemaSnapshot:
        """Ensure the cache for a profile is fresh and return the snapshot."""
        cache = self.get_cache(profile_id)
        cache.ensure_fresh(table_filter)
        return cache.snapshot

    def invalidate(self, profile_id: str):
        """Force a refresh of the cache for a profile."""
        if profile_id in self._caches:
            self._caches[profile_id]._last_refresh = 0

    @property
    def snapshot(self) -> Optional[SchemaSnapshot]:
        """Convenience: get snapshot for the currently connected profile."""
        if db.current_profile:
            cache = self.get_cache(db.current_profile.id)
            return cache.snapshot
        return None


def build_schema_snapshot(db_name: str, fast_mode: bool = False, progress_callback=None,
                         table_filter: list = None) -> SchemaSnapshot:
    """Query database metadata and build a complete schema snapshot.

    Args:
        db_name: Database name.
        fast_mode: If True, skip sample data and value distribution queries.
                   Only tables, columns, FKs, indexes, and checksums are built.
                   Fast mode is ~10x faster for large databases.
        progress_callback: Optional callable(stage, current, total) for progress reporting.
        table_filter: Optional list of table names — only these tables are included.

    Works with MySQL, PostgreSQL, and Dameng.
    """
    # Always apply global filter as fallback if no explicit filter passed
    if table_filter is None:
        table_filter = _active_table_filter

    is_pg = (db.db_type == "postgresql")
    is_dm = (db.db_type in ("dameng", "dm", "dm8"))
    quote = db.quote
    tables = {}

    # Schema filter clause
    if is_pg:
        schema_filter = "TABLE_SCHEMA NOT IN ('pg_catalog', 'information_schema')"
        schema_filter_single = schema_filter
    elif is_dm:
        schema_filter = "OWNER = USER"
        schema_filter_single = schema_filter
        # Dameng uses Oracle-style ALL_ views
    else:
        schema_filter = "TABLE_SCHEMA = %s"
        schema_filter_single = schema_filter

    # 1. Get table list + row counts
    with db.cursor() as cursor:
        if is_dm:
            cursor.execute(
                "SELECT TABLE_NAME, "
                "'BASE TABLE' AS TABLE_TYPE, "
                "NUM_ROWS AS TABLE_ROWS, "
                "COALESCE((SELECT COMMENTS FROM ALL_TAB_COMMENTS "
                "WHERE TABLE_NAME = t.TABLE_NAME AND OWNER = t.OWNER), '') AS TABLE_COMMENT "
                "FROM ALL_TABLES t "
                "WHERE TABLE_NAME NOT LIKE '##%' "
                "AND TABLE_NAME NOT LIKE 'SREF%' "
                "AND OWNER = USER "
                "ORDER BY TABLE_NAME"
            )
        elif is_pg:
            cursor.execute(
                "SELECT tablename AS TABLE_NAME, "
                "COALESCE(obj_description(pc.oid), '') AS TABLE_COMMENT, "
                "COALESCE(n_live_tup, 0) AS TABLE_ROWS "
                "FROM pg_catalog.pg_tables t "
                "LEFT JOIN pg_catalog.pg_class pc ON pc.relname = t.tablename "
                "LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relname = t.tablename "
                "WHERE t.schemaname NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY t.tablename"
            )
        else:
            cursor.execute(
                "SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS "
                "FROM information_schema.TABLES "
                f"WHERE {schema_filter} AND TABLE_TYPE = 'BASE TABLE' "
                "ORDER BY TABLE_NAME",
                (db_name,) if not is_pg else (),
            )
        table_rows = cursor.fetchall()

    # Apply table filter — only include selected tables
    if table_filter is not None:
        filter_set = {t.lower() for t in table_filter}
        table_rows = [tr for tr in table_rows if tr["TABLE_NAME"].lower() in filter_set]

    for tr in table_rows:
        tables[tr["TABLE_NAME"]] = TableMeta(
            name=tr["TABLE_NAME"],
            comment=tr.get("TABLE_COMMENT", "") or "",
            row_count=tr.get("TABLE_ROWS", 0) or 0,
        )

    # Dameng: ALL_TABLES.NUM_ROWS is 0 if stats not gathered — fix with COUNT(*)
    if is_dm and fast_mode:
        zero_row_tables = [tname for tname, meta in tables.items()
                          if meta.row_count == 0 or meta.row_count is None]
        # Prioritize: filtered tables first, then FK-connected, cap at 50 to avoid timeout
        if table_filter:
            zero_row_tables = sorted(zero_row_tables,
                key=lambda t: (0 if t in set(table_filter) else 1, t))
        zero_row_tables = zero_row_tables[:50]
        for tname in zero_row_tables:
            try:
                with db.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) AS cnt FROM {quote(tname)}")
                    cnt_row = cur.fetchone()
                    if cnt_row and cnt_row.get("cnt", 0) > 0:
                        tables[tname].row_count = cnt_row["cnt"]
            except Exception:
                pass  # Table might be inaccessible — keep row_count=0

    # 2. Get column metadata
    with db.cursor() as cursor:
        if is_dm:
            cursor.execute(
                "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, "
                "c.DATA_TYPE || '(' || c.DATA_LENGTH || ')' AS COLUMN_TYPE, "
                "c.NULLABLE AS IS_NULLABLE, "
                "CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 'PRI' ELSE '' END AS COLUMN_KEY, "
                "c.DATA_DEFAULT AS COLUMN_DEFAULT, "
                "COALESCE(cm.COMMENTS, '') AS COLUMN_COMMENT, "
                "c.COLUMN_ID AS ORDINAL_POSITION "
                "FROM ALL_TAB_COLUMNS c "
                "LEFT JOIN ALL_COL_COMMENTS cm "
                "ON c.TABLE_NAME = cm.TABLE_NAME AND c.COLUMN_NAME = cm.COLUMN_NAME AND c.OWNER = cm.OWNER "
                "LEFT JOIN ("
                "SELECT acc.TABLE_NAME, acc.COLUMN_NAME "
                "FROM ALL_CONS_COLUMNS acc "
                "JOIN ALL_CONSTRAINTS ac ON acc.CONSTRAINT_NAME = ac.CONSTRAINT_NAME "
                "AND acc.OWNER = ac.OWNER "
                "WHERE ac.CONSTRAINT_TYPE = 'P' AND acc.OWNER = USER"
                ") pk ON c.TABLE_NAME = pk.TABLE_NAME AND c.COLUMN_NAME = pk.COLUMN_NAME "
                "WHERE c.OWNER = USER "
                "AND c.TABLE_NAME NOT LIKE '##%' "
                "ORDER BY c.TABLE_NAME, c.COLUMN_ID"
            )
        elif is_pg:
            cursor.execute(
                "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, "
                "CASE WHEN c.CHARACTER_MAXIMUM_LENGTH IS NOT NULL "
                "THEN c.DATA_TYPE || '(' || c.CHARACTER_MAXIMUM_LENGTH || ')' "
                "WHEN c.NUMERIC_PRECISION IS NOT NULL "
                "THEN c.DATA_TYPE || '(' || c.NUMERIC_PRECISION || ',' || COALESCE(c.NUMERIC_SCALE,0) || ')' "
                "ELSE c.DATA_TYPE END AS COLUMN_TYPE, "
                "c.IS_NULLABLE, "
                "COALESCE(tc.CONSTRAINT_TYPE, '') AS COLUMN_KEY, "
                "c.COLUMN_DEFAULT, "
                "COALESCE(pg_catalog.col_description(pc.oid, c.ORDINAL_POSITION), '') AS COLUMN_COMMENT, "
                "c.ORDINAL_POSITION "
                "FROM information_schema.COLUMNS c "
                "LEFT JOIN pg_catalog.pg_class pc ON pc.relname = c.TABLE_NAME "
                "AND pc.relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = c.TABLE_SCHEMA) "
                "LEFT JOIN information_schema.KEY_COLUMN_USAGE kcu "
                "ON kcu.TABLE_NAME = c.TABLE_NAME AND kcu.COLUMN_NAME = c.COLUMN_NAME "
                "AND kcu.TABLE_SCHEMA = c.TABLE_SCHEMA "
                "LEFT JOIN information_schema.TABLE_CONSTRAINTS tc "
                "ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                "AND tc.TABLE_SCHEMA = c.TABLE_SCHEMA AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY' "
                "WHERE c.TABLE_SCHEMA NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION"
            )
        else:
            cursor.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, "
                "IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, COLUMN_COMMENT "
                "FROM information_schema.COLUMNS "
                f"WHERE {schema_filter} "
                "ORDER BY TABLE_NAME, ORDINAL_POSITION",
                (db_name,) if not is_pg else (),
            )
        col_rows = cursor.fetchall()

    for cr in col_rows:
        tname = cr["TABLE_NAME"]
        if tname in tables:
            col_type = cr.get("COLUMN_TYPE") or cr.get("DATA_TYPE", "unknown")
            nullable = cr.get("IS_NULLABLE", "YES")
            nullable = nullable == "YES" if isinstance(nullable, str) else bool(nullable)
            tables[tname].columns.append(ColumnMeta(
                name=cr["COLUMN_NAME"],
                data_type=cr.get("DATA_TYPE", ""),
                column_type=col_type,
                nullable=nullable,
                key=cr.get("COLUMN_KEY", "") or "",
                default=str(cr.get("COLUMN_DEFAULT") or ""),
                comment=cr.get("COLUMN_COMMENT", "") or "",
            ))

    # 3. Get foreign keys
    foreign_keys = []
    with db.cursor() as cursor:
        if is_dm:
            cursor.execute(
                "SELECT ac.CONSTRAINT_NAME, ac.TABLE_NAME, "
                "acc.COLUMN_NAME, "
                "ac_r.TABLE_NAME AS REFERENCED_TABLE_NAME, "
                "acc_r.COLUMN_NAME AS REFERENCED_COLUMN_NAME, "
                "ac.DELETE_RULE, '' AS UPDATE_RULE "
                "FROM ALL_CONSTRAINTS ac "
                "JOIN ALL_CONS_COLUMNS acc "
                "ON ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME AND ac.OWNER = acc.OWNER "
                "LEFT JOIN ALL_CONSTRAINTS ac_r "
                "ON ac.R_CONSTRAINT_NAME = ac_r.CONSTRAINT_NAME AND ac.R_OWNER = ac_r.OWNER "
                "LEFT JOIN ALL_CONS_COLUMNS acc_r "
                "ON ac_r.CONSTRAINT_NAME = acc_r.CONSTRAINT_NAME AND ac_r.OWNER = acc_r.OWNER "
                "WHERE ac.CONSTRAINT_TYPE = 'R' AND ac.OWNER = USER"
            )
        elif is_pg:
            cursor.execute(
                "SELECT tc.CONSTRAINT_NAME, tc.TABLE_NAME, "
                "kcu.COLUMN_NAME, "
                "ccu.TABLE_NAME AS REFERENCED_TABLE_NAME, "
                "ccu.COLUMN_NAME AS REFERENCED_COLUMN_NAME, "
                "rc.DELETE_RULE, rc.UPDATE_RULE "
                "FROM information_schema.TABLE_CONSTRAINTS tc "
                "JOIN information_schema.KEY_COLUMN_USAGE kcu "
                "ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME "
                "JOIN information_schema.REFERENTIAL_CONSTRAINTS rc "
                "ON tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME "
                "JOIN information_schema.CONSTRAINT_COLUMN_USAGE ccu "
                "ON rc.UNIQUE_CONSTRAINT_NAME = ccu.CONSTRAINT_NAME "
                "WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY'"
            )
        else:
            cursor.execute(
                "SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, "
                "REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                f"WHERE {schema_filter_single} AND REFERENCED_TABLE_NAME IS NOT NULL",
                (db_name,) if not is_pg else (),
            )
        fk_rows = cursor.fetchall()

    processed_fks = set()
    for fk in fk_rows:
        cname = fk["CONSTRAINT_NAME"]
        if cname in processed_fks:
            continue
        processed_fks.add(cname)

        delete_rule = fk.get("DELETE_RULE", "") or ""
        update_rule = fk.get("UPDATE_RULE", "") or ""

        # For MySQL, query the rules separately
        if not is_pg and not delete_rule:
            try:
                with db.cursor() as cur2:
                    cur2.execute(
                        "SELECT DELETE_RULE, UPDATE_RULE "
                        "FROM information_schema.REFERENTIAL_CONSTRAINTS "
                        "WHERE CONSTRAINT_SCHEMA = %s AND CONSTRAINT_NAME = %s",
                        (db_name, cname),
                    )
                    rule = cur2.fetchone()
                    if rule:
                        delete_rule = rule.get("DELETE_RULE", "") or ""
                        update_rule = rule.get("UPDATE_RULE", "") or ""
            except Exception:
                pass

        fk_meta = ForeignKeyMeta(
            constraint_name=cname,
            table_name=fk["TABLE_NAME"],
            column_name=fk["COLUMN_NAME"],
            referenced_table=fk["REFERENCED_TABLE_NAME"],
            referenced_column=fk["REFERENCED_COLUMN_NAME"],
            delete_rule=delete_rule,
            update_rule=update_rule,
        )
        foreign_keys.append(fk_meta)
        if fk["TABLE_NAME"] in tables:
            tables[fk["TABLE_NAME"]].foreign_keys.append(cname)

    # 4. Get indexes
    with db.cursor() as cursor:
        if is_dm:
            cursor.execute(
                "SELECT ai.TABLE_NAME, ai.INDEX_NAME, aic.COLUMN_NAME, "
                "CASE WHEN ai.UNIQUENESS = 'UNIQUE' THEN 0 ELSE 1 END AS NON_UNIQUE "
                "FROM ALL_INDEXES ai "
                "JOIN ALL_IND_COLUMNS aic "
                "ON ai.INDEX_NAME = aic.INDEX_NAME AND ai.TABLE_OWNER = aic.INDEX_OWNER "
                "WHERE ai.TABLE_OWNER = USER AND ai.TABLE_NAME IN "
                "(SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = USER) "
                "ORDER BY ai.TABLE_NAME, ai.INDEX_NAME, aic.COLUMN_POSITION"
            )
        elif is_pg:
            cursor.execute(
                "SELECT tablename AS TABLE_NAME, indexname AS INDEX_NAME, "
                "indexdef AS INDEX_DEF "
                "FROM pg_catalog.pg_indexes "
                "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY tablename, indexname"
            )
            for idx in cursor.fetchall():
                tname = idx["TABLE_NAME"]
                if tname in tables:
                    is_unique = "UNIQUE" in idx.get("INDEX_DEF", "").upper()
                    tables[tname].indexes.append({
                        "name": idx["INDEX_NAME"],
                        "column": "",
                        "unique": is_unique,
                    })
        else:
            cursor.execute(
                "SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME, NON_UNIQUE "
                "FROM information_schema.STATISTICS "
                f"WHERE {schema_filter} "
                "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
                (db_name,) if not is_pg else (),
            )
            for idx in cursor.fetchall():
                tname = idx["TABLE_NAME"]
                if tname in tables:
                    tables[tname].indexes.append({
                        "name": idx["INDEX_NAME"],
                        "column": idx["COLUMN_NAME"],
                        "unique": idx["NON_UNIQUE"] == 0,
                    })

    # 5. Sample data from each table (also fix row counts for Dameng)
    # SKIP in fast mode — samples are loaded lazily on demand
    if not fast_mode:
        _load_table_samples(tables, is_pg, is_dm, quote)

    # 6. Build generic summary (with or without value distributions)
    summary = _build_generic_summary(tables, foreign_keys, db_name, is_pg or is_dm, fast_mode)

    # 7. Compute checksums
    with db.cursor() as cursor:
        if is_dm:
            cursor.execute(
                "SELECT TABLE_NAME, CAST(NUM_ROWS AS VARCHAR) AS TS "
                "FROM ALL_TABLES WHERE OWNER = USER"
            )
        elif is_pg:
            # PG: use relfilenode change counter + last vacuum/analyze time
            cursor.execute(
                "SELECT t.tablename AS TABLE_NAME, "
                "pg_catalog.pg_relation_size(pc.oid)::TEXT AS ts "
                "FROM pg_catalog.pg_tables t "
                "JOIN pg_catalog.pg_class pc ON pc.relname = t.tablename "
                "AND pc.relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = t.schemaname) "
                "WHERE t.schemaname NOT IN ('pg_catalog', 'information_schema')"
            )
        else:
            cursor.execute(
                "SELECT TABLE_NAME, COALESCE(UPDATE_TIME, CREATE_TIME) AS ts "
                "FROM information_schema.TABLES "
                f"WHERE {schema_filter}",
                (db_name,),
            )
        ts_rows = cursor.fetchall()
    raw = "|".join(f"{r['TABLE_NAME']}={r['ts']}" for r in sorted(ts_rows, key=lambda r: r['TABLE_NAME']))
    checksums = {"info_schema_md5": hashlib.md5(raw.encode()).hexdigest()}

    return SchemaSnapshot(
        captured_at=datetime.now().isoformat(),
        db_name=db_name,
        tables=tables,
        foreign_keys=foreign_keys,
        checksums=checksums,
        summary=summary,
    )


def _load_table_samples(tables: dict, is_pg: bool, is_dm: bool, quote):
    """Load sample data and fix row counts for all tables (slow — skip in fast mode)."""
    for tname, meta in tables.items():
        try:
            with db.cursor() as cursor:
                # Dameng: fix row count if NUM_ROWS is 0 (stats not gathered)
                if is_dm and (meta.row_count == 0 or meta.row_count is None):
                    try:
                        cursor.execute(f"SELECT COUNT(*) AS cnt FROM {quote(tname)}")
                        cnt_row = cursor.fetchone()
                        if cnt_row and cnt_row["cnt"] is not None:
                            meta.row_count = cnt_row["cnt"]
                    except Exception:
                        pass

                cursor.execute(f"SELECT * FROM {quote(tname)} LIMIT 3")
                if cursor.description:
                    if is_pg:
                        sample_cols = [col.name for col in cursor.description]
                    else:
                        sample_cols = [col[0] for col in cursor.description]
                    sample_rows = cursor.fetchall()
                    meta.sample_rows = [
                        {col: row[col] for col in sample_cols}
                        for row in sample_rows
                    ]
        except Exception:
            meta.sample_rows = []


def ensure_table_samples(table_name: str) -> bool:
    """Lazy-load sample data for a specific table into the cached snapshot.

    Call this before get_table_detail or analyze_column to ensure
    the table has sample data available.

    Returns True if samples were loaded, False if table not found or no data.
    """
    if not db.current_profile:
        return False
    cache = schema_manager.get_cache(db.current_profile.id)
    snap = cache.snapshot
    if snap is None or table_name not in snap.tables:
        return False

    meta = snap.tables[table_name]
    # Already has samples
    if meta.sample_rows:
        return True

    # Load samples for this table
    is_pg = (db.db_type == "postgresql")
    is_dm = (db.db_type in ("dameng", "dm", "dm8"))
    q = db.quote

    try:
        with db.cursor() as cursor:
            # Fix row count for Dameng
            if is_dm and (meta.row_count == 0 or meta.row_count is None):
                try:
                    cursor.execute(f"SELECT COUNT(*) AS cnt FROM {q(table_name)}")
                    cnt_row = cursor.fetchone()
                    if cnt_row and cnt_row["cnt"] is not None:
                        meta.row_count = cnt_row["cnt"]
                except Exception:
                    pass

            cursor.execute(f"SELECT * FROM {q(table_name)} LIMIT 3")
            if cursor.description:
                if is_pg:
                    sample_cols = [col.name for col in cursor.description]
                else:
                    sample_cols = [col[0] for col in cursor.description]
                sample_rows = cursor.fetchall()
                meta.sample_rows = [
                    {col: row[col] for col in sample_cols}
                    for row in sample_rows
                ]
        return bool(meta.sample_rows)
    except Exception:
        return False


def _build_generic_summary(tables: dict, foreign_keys: list, db_name: str, is_pg: bool = False, fast_mode: bool = False) -> GenericSummary:
    """Build a generic summary — auto-discovers value distributions for all tables and columns."""
    total_rows = sum(t.row_count for t in tables.values())
    summary = GenericSummary(
        total_tables=len(tables),
        total_rows_estimate=total_rows,
        foreign_key_count=len(foreign_keys),
    )

    for tname, meta in tables.items():
        col_list = [c.name for c in meta.columns]
        sample_values = {}

        # In fast mode, skip value distribution queries entirely
        if fast_mode or meta.row_count == 0:
            summary.table_summaries[tname] = {
                "row_count": meta.row_count,
                "columns": col_list,
                "sample_values": sample_values,
            }
            continue

        # Limit: sample at most 5 columns per table, prioritize PK/FK columns
        # For very large tables (>100k rows), reduce to 2 columns to avoid expensive scans
        is_large_table = meta.row_count > 100000
        max_sample_cols = 2 if is_large_table else 5
        priority_cols = []
        other_cols = []
        for col in meta.columns:
            dt = col.data_type.lower() if col.data_type else ""
            if dt not in ("varchar", "varchar2", "nvarchar2", "char", "enum", "tinyint", "smallint", "int", "bigint", "integer"):
                continue
            if col.key or col.name.upper().endswith('_ID'):
                priority_cols.append(col)
            else:
                other_cols.append(col)
        cols_to_sample = (priority_cols + other_cols)[:max_sample_cols]

        q = db.quote
        is_dm = (db.db_type in ("dameng", "dm", "dm8"))
        for col in cols_to_sample:
            try:
                with db.cursor() as cursor:
                    # Set per-query timeout to avoid hanging on unindexed columns
                    if not is_pg and not is_dm:
                        try:
                            cursor.execute("SET SESSION MAX_EXECUTION_TIME = 3000")
                        except Exception:
                            pass  # Older MySQL may not support this
                    elif is_pg:
                        try:
                            cursor.execute("SET LOCAL statement_timeout = '3s'")
                        except Exception:
                            pass
                    cursor.execute(
                        f"SELECT DISTINCT {q(col.name)} FROM {q(tname)} "
                        f"WHERE {q(col.name)} IS NOT NULL LIMIT 8"
                    )
                    vals = [str(row[col.name]) for row in cursor.fetchall()]
                    if vals and len(vals) <= 8:
                        sample_values[col.name] = vals
            except Exception:
                pass

        summary.table_summaries[tname] = {
            "row_count": meta.row_count,
            "columns": col_list,
            "sample_values": sample_values,
        }

        # Build column profiles from sample data
        col_profiles = {}
        if meta.sample_rows:
            for col in meta.columns:
                null_count = sum(1 for r in meta.sample_rows if r.get(col.name) is None)
                null_ratio = null_count / len(meta.sample_rows) if meta.sample_rows else 0
                vals = [str(r[col.name])[:40] for r in meta.sample_rows if r.get(col.name) is not None]
                # Determine value type
                if not vals:
                    vtype = "全NULL ⚠️"
                elif all(v.replace('.','',1).replace('-','',1).isdigit() for v in vals if v):
                    vtype = "数值"
                elif all(len(v) <= 3 and v.isascii() for v in vals):
                    vtype = "编码/缩写"
                elif any('一' <= c <= '鿿' for v in vals for c in v):
                    vtype = "中文文本"
                else:
                    vtype = "混合"
                col_profiles[col.name] = {
                    "null_ratio": round(null_ratio, 1),
                    "value_type": vtype,
                    "sample_vals": vals[:3],
                }
        summary.column_profiles[tname] = col_profiles

    return summary


# Global schema cache manager
schema_manager = SchemaCacheManager()

# Legacy compatibility — delegate to the current profile's cache
class _LegacySchemaCacheProxy:
    """Proxy that delegates to SchemaCacheManager for backward compat."""
    @property
    def snapshot(self):
        if db.current_profile:
            cache = schema_manager.get_cache(db.current_profile.id)
            return cache.snapshot
        return None

    def ensure_fresh(self):
        if db.current_profile:
            cache = schema_manager.get_cache(db.current_profile.id)
            cache.ensure_fresh()

    def load_from_disk(self) -> bool:
        if db.current_profile:
            cache = schema_manager.get_cache(db.current_profile.id)
            return cache.load_from_disk()
        return False


schema_cache = _LegacySchemaCacheProxy()
