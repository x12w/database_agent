"""Background database explorer — incrementally explores large databases.

For small databases (≤20 tables): runs full LLM-driven exploration immediately.
For large databases (>20 tables): runs fast schema build + starts background
exploration that prioritizes important tables and completes incrementally.

The detailed cache (schema_{id}_detailed.json) tracks exploration progress.
"""
import json
import os
import re
import threading
import time
from app.database.connection import db
from app.database.schema_cache import schema_manager, build_schema_snapshot, ensure_table_samples

DETAILED_CACHE_DIR = "data/detailed_cache"
# Threshold: databases with more tables than this use background exploration
LARGE_DB_THRESHOLD = 20
# How many high-priority tables to sample during initial fast exploration
FAST_EXPLORE_SAMPLE_COUNT = 10


def _score_table(tname: str, meta, fk_count: dict) -> float:
    """Score a table for exploration priority. Higher = explore first.

    Factors:
    - FK relationships (connected tables are more interesting)
    - Has data (empty tables are less useful)
    - Table name suggests business relevance
    """
    score = 0.0

    # FK relationships: +10 per FK connection
    fk_in = fk_count.get(f"in:{tname}", 0)
    fk_out = fk_count.get(f"out:{tname}", 0)
    score += (fk_in + fk_out) * 10.0

    # Has data: +5 if rows > 0
    if meta.row_count and meta.row_count > 0:
        score += 5.0

    # Table name heuristics
    name_lower = tname.lower()
    # Penalize likely system/utility tables
    for penalty_pattern in ['log_', 'audit_', 'tmp_', '_bak', '_history', '_archive',
                             'sys_', '__', '##', 'sref_', 'migrate_', '_log', '_tmp']:
        if penalty_pattern in name_lower:
            score -= 10.0
            break
    # Reward likely business entities
    for bonus_pattern in ['employee', 'user', 'customer', 'order', 'product',
                           'department', 'company', 'student', 'course', 'exam',
                           'training', 'salary', 'contract', 'project', 'task']:
        if bonus_pattern in name_lower:
            score += 3.0
            break

    return score


def _analyze_fk_counts(foreign_keys: list) -> dict:
    """Count incoming and outgoing FK references per table."""
    counts = {}
    for fk in foreign_keys:
        key_out = f"out:{fk.table_name}"
        key_in = f"in:{fk.referenced_table}"
        counts[key_out] = counts.get(key_out, 0) + 1
        counts[key_in] = counts.get(key_in, 0) + 1
    return counts


def get_exploration_report(profile_id: str) -> str:
    """Get any existing exploration report for a profile."""
    # Check detailed cache first
    path = os.path.join(DETAILED_CACHE_DIR, f"{profile_id}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("report", "")
        except Exception:
            pass
    # Fall back to legacy exploration file
    legacy_path = os.path.join("data/explorations", f"{profile_id}.json")
    if os.path.exists(legacy_path):
        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("report", "")
        except Exception:
            pass
    return ""


class BackgroundExplorer:
    """Manages background exploration tasks for large databases."""

    def __init__(self):
        self._tasks: dict[str, dict] = {}  # profile_id -> status dict

    def start(self, profile_id: str, tables: dict, foreign_keys: list):
        """Start background exploration for a profile.

        Runs in a daemon thread. Skips if already running or complete.
        """
        if profile_id in self._tasks and not self._tasks[profile_id].get("done"):
            return  # Already running

        # Score and sort tables by priority
        fk_counts = _analyze_fk_counts(foreign_keys)
        scored = []
        for tname, meta in tables.items():
            score = _score_table(tname, meta, fk_counts)
            scored.append((tname, score))
        scored.sort(key=lambda x: x[1], reverse=True)

        status = {
            "done": False,
            "profile_id": profile_id,
            "total_tables": len(tables),
            "sampled_tables": 0,
            "priority_order": [t for t, _ in scored],
            "started_at": time.time(),
        }
        self._tasks[profile_id] = status

        thread = threading.Thread(
            target=self._run,
            args=(profile_id, tables, foreign_keys, scored),
            daemon=True,
        )
        thread.start()

    def get_progress(self, profile_id: str) -> dict:
        """Get exploration progress for a profile."""
        if profile_id not in self._tasks:
            # Check detailed cache
            path = os.path.join(DETAILED_CACHE_DIR, f"{profile_id}.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    return {
                        "done": data.get("completion", 0) >= 1.0,
                        "total_tables": data.get("total_tables", 0),
                        "sampled_tables": data.get("sampled_tables", 0),
                        "completion": data.get("completion", 0),
                    }
                except Exception:
                    pass
            return {"done": False, "total_tables": 0, "sampled_tables": 0, "completion": 0}

        status = self._tasks[profile_id]
        total = status["total_tables"]
        sampled = status["sampled_tables"]
        return {
            "done": status["done"],
            "total_tables": total,
            "sampled_tables": sampled,
            "completion": sampled / max(total, 1),
        }

    def _run(self, profile_id: str, tables: dict, foreign_keys: list, scored: list):
        """Background exploration thread — process tables in priority order."""
        status = self._tasks.get(profile_id)
        if not status:
            return

        try:
            # Re-connect for thread safety (each thread needs its own connection)
            from app.config import config
            for profile in config.load_profiles():
                if profile.id == profile_id:
                    db.connect_to(profile)
                    break

            report_lines = []
            processed = set()
            batch_size = 5  # Process in small batches, updating cache after each

            for i, (tname, score) in enumerate(scored):
                if status.get("done"):  # Allow cancellation
                    break

                try:
                    # Load samples for this table
                    ok = ensure_table_samples(tname)
                    if ok:
                        meta = tables.get(tname)
                        if meta and meta.sample_rows:
                            # Generate a brief table summary for the report
                            col_names = [c.name for c in meta.columns[:8]]
                            report_lines.append(
                                f"- **{tname}** ({meta.row_count} 行): "
                                f"{', '.join(col_names)}"
                            )
                    processed.add(tname)
                    status["sampled_tables"] = len(processed)

                except Exception as e:
                    print(f"[BackgroundExplorer] Error sampling {tname}: {e}")
                    processed.add(tname)
                    status["sampled_tables"] = len(processed)

                # Persist progress after each batch
                if (i + 1) % batch_size == 0:
                    self._save_progress(profile_id, tables, foreign_keys,
                                        processed, report_lines, status)
                    time.sleep(0.5)  # Small delay to avoid hammering the DB

            # Final save
            status["done"] = True
            self._save_progress(profile_id, tables, foreign_keys,
                                processed, report_lines, status)

        except Exception as e:
            print(f"[BackgroundExplorer] Fatal error for {profile_id}: {e}")
            import traceback
            traceback.print_exc()
            status["done"] = True

    def _save_progress(self, profile_id: str, tables: dict, foreign_keys: list,
                       processed: set, report_lines: list, status: dict):
        """Persist exploration progress to detailed cache file AND main schema cache."""
        os.makedirs(DETAILED_CACHE_DIR, exist_ok=True)

        total = status["total_tables"]
        sampled = len(processed)

        # Build table summaries for processed tables
        table_summaries = {}
        for tname in processed:
            meta = tables.get(tname)
            if meta:
                table_summaries[tname] = {
                    "columns": [c.name for c in meta.columns],
                    "row_count": meta.row_count,
                    "sample_rows": meta.sample_rows[:3] if meta.sample_rows else [],
                }

        data = {
            "profile_id": profile_id,
            "db_name": db.current_db_name,
            "explored_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "completion": sampled / max(total, 1),
            "total_tables": total,
            "sampled_tables": sampled,
            "priority_order": status.get("priority_order", []),
            "table_summaries": table_summaries,
            "report": "\n".join(report_lines) if report_lines else "",
        }

        path = os.path.join(DETAILED_CACHE_DIR, f"{profile_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        # Also persist the main schema cache so samples survive restarts
        try:
            schema_manager.get_cache(profile_id).persist()
        except Exception as e:
            print(f"[BackgroundExplorer] Failed to persist main cache: {e}")


# Singleton
background_explorer = BackgroundExplorer()
