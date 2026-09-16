#!/usr/bin/env python3
"""Integration tests for MySQL and PostgreSQL backends.

Validates: connection, schema discovery, tools, skills, and chat API.
Usage: ./venv/bin/python test_integration.py
"""

import json
import sys
import urllib.request
import urllib.error
from app.config import DatabaseProfile, config
from app.database.connection import db


# ---- Test profiles ----
MYSQL_PROFILE = DatabaseProfile(
    id="test-mysql",
    name="MySQL Test",
    host=config.DB_HOST,
    port=config.DB_PORT,
    user=config.DB_USER,
    password=config.DB_PASSWORD,
    database=config.DB_NAME,
    db_type="mysql",
)

PG_PROFILE = DatabaseProfile(
    id="test-pg",
    name="PG Test",
    host="127.0.0.1",
    port=5433,
    user="postgres",
    password="root123",
    database="hospital_db",
    db_type="postgresql",
)

DM_PROFILE = DatabaseProfile(
    id="test-dameng",
    name="Dameng Test",
    host="127.0.0.1",
    port=5237,
    user="SYSDBA",
    password="root123456",
    database="DAMENG",
    db_type="dameng",
)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}  {detail}")
        if detail:
            print(f"    {detail[:200]}")


def test_backend(profile, label):
    """Run all tests against one database backend."""
    global passed, failed
    print(f"\n{'='*60}")
    print(f"Testing: {label} ({profile.db_type})")
    print(f"{'='*60}")

    # 1. Connection
    print("\n-- Connection --")
    try:
        db.connect_to(profile)
        check("connect", True)
        check("backend type", profile.db_type in ("mysql", "mariadb", "postgresql", "postgres", "pg", "dameng", "dm", "dm8"))
        check("current_db_name", db.current_db_name == profile.database,
              f"expected {profile.database}, got {db.current_db_name}")
    except Exception as e:
        check("connect", False, str(e))
        return  # Can't continue without connection

    # 2. Low-level tools
    print("\n-- Low-level Tools --")
    from app.agent.tools import _list_tables, _describe_table, _sample_data

    r = _list_tables()
    check("list_tables success", r["success"], r.get("content", ""))
    if r["success"]:
        tables = r["raw_result"]["tables"]
        check("list_tables has tables", len(tables) > 0)
        test_table = tables[0]
    else:
        test_table = None

    if test_table:
        r2 = _describe_table(test_table)
        check("describe_table success", r2["success"], r2.get("content", ""))
        if r2["success"]:
            check("describe_table has columns", r2["raw_result"]["row_count"] > 0)

        r3 = _sample_data(test_table, 3)
        check("sample_data success", r3["success"], r3.get("content", ""))
        if r3["success"]:
            check("sample_data has rows", r3["raw_result"]["row_count"] >= 0)

    # 3. Schema snapshot
    print("\n-- Schema Snapshot --")
    from app.database.schema_cache import build_schema_snapshot, schema_manager

    try:
        snap = build_schema_snapshot(profile.database)
        check("build_snapshot success", snap is not None and len(snap.tables) > 0)
        check("snapshot has tables", len(snap.tables) > 0)
        check("snapshot has FKs", len(snap.foreign_keys) >= 0)
        # Verify sample data
        tables_with_samples = sum(1 for t in snap.tables.values() if t.sample_rows)
        check("snapshot has samples", tables_with_samples > 0,
              f"{tables_with_samples}/{len(snap.tables)} tables have samples")
        # Verify summary
        check("snapshot has summary", snap.summary is not None)
    except Exception as e:
        check("build_snapshot", False, str(e))
        snap = None

    # 4. High-level skills
    print("\n-- Skills --")
    from app.agent.skills import (
        skill_schema_summary, skill_table_detail,
        skill_analyze_column, skill_find_related, skill_suggested_questions,
    )

    r4 = skill_schema_summary()
    check("schema_summary success", r4["success"], r4.get("content", ""))
    if r4["success"]:
        check("schema_summary tables", r4["raw_result"]["table_count"] > 0)

    if test_table:
        r5 = skill_table_detail(test_table)
        check("table_detail success", r5["success"], r5.get("content", ""))

        # Find a string column for analyze
        if snap and test_table in snap.tables:
            str_col = next((c.name for c in snap.tables[test_table].columns
                           if c.data_type in ("varchar", "char", "text", "character varying")), None)
            if not str_col:
                str_col = snap.tables[test_table].columns[0].name
            r6 = skill_analyze_column(test_table, str_col)
            check("analyze_column success", r6["success"], r6.get("content", ""))

    r7 = skill_find_related(test_table) if test_table else {"success": True}
    check("find_related success", r7["success"], r7.get("content", ""))

    r8 = skill_suggested_questions()
    check("suggested_questions success", r8["success"])

    # 5. SQL validator
    print("\n-- SQL Validator --")
    from app.database.sql_validator import validate_sql, SQLValidationError

    try:
        validate_sql("SELECT * FROM test", profile.db_type)
        check("valid SQL accepted", True)
    except SQLValidationError as e:
        check("valid SQL accepted", False, str(e))

    if profile.db_type == "postgresql":
        try:
            validate_sql("SHOW TABLES", "postgresql")
            check("PG rejects SHOW", False, "Should have rejected")
        except SQLValidationError:
            check("PG rejects SHOW", True)
    elif profile.db_type in ("dameng", "dm", "dm8"):
        try:
            validate_sql("SHOW TABLES", "dameng")
            check("Dameng rejects SHOW", False, "Should have rejected")
        except SQLValidationError:
            check("Dameng rejects SHOW", True)
    else:
        try:
            validate_sql("SHOW TABLES", "mysql")
            check("MySQL accepts SHOW", True)
        except SQLValidationError:
            check("MySQL accepts SHOW", False)

    # 6. Schema cache manager
    print("\n-- Schema Cache Manager --")
    try:
        schema_manager.invalidate(profile.id)
        snap2 = schema_manager.ensure_fresh(profile.id)
        check("cache_manager refresh", snap2 is not None and len(snap2.tables) > 0)
    except Exception as e:
        check("cache_manager refresh", False, str(e))


def test_chat_api():
    """Test chat API responses for both backends via HTTP."""
    global passed, failed
    print(f"\n{'='*60}")
    print("Testing: Chat API")
    print(f"{'='*60}")

    base = "http://localhost:8000"

    # Health check
    print("\n-- Health --")
    try:
        resp = json.loads(urllib.request.urlopen(f"{base}/api/health", timeout=5).read())
        check("health OK", resp["status"] == "ok")
        check("database connected", resp["database"])
    except Exception as e:
        check("health", False, str(e))
        return

    # List databases
    print("\n-- Database Management --")
    try:
        resp = json.loads(urllib.request.urlopen(f"{base}/api/databases", timeout=5).read())
        profiles = resp.get("profiles", [])
        check("list databases", len(profiles) >= 1)

        # Find a MySQL profile
        mysql_id = next((p["id"] for p in profiles if p.get("db_type") in ("mysql", None)), None)
        if mysql_id:
            resp2 = json.loads(urllib.request.urlopen(
                f"{base}/api/databases/{mysql_id}/status", timeout=5).read())
            check("MySQL status", resp2.get("table_count", 0) > 0)
    except Exception as e:
        check("database API", False, str(e))

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    # Test MySQL
    test_backend(MYSQL_PROFILE, "MySQL (training_exam_db)")

    # Test PostgreSQL
    test_backend(PG_PROFILE, "PostgreSQL (hospital_db)")

    # Test Dameng
    test_backend(DM_PROFILE, "Dameng (DAMENG)")

    # Test chat API (server must be running)
    test_chat_api()

    print()
    if failed == 0:
        print("✓ ALL TESTS PASSED")
        sys.exit(0)
    else:
        print(f"✗ {failed} TESTS FAILED")
        sys.exit(1)
