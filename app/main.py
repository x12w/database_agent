"""FastAPI application — serves the web UI, SSE streaming, and database management API."""

import json
import os
import uuid

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import config, DatabaseProfile
from app.agent.orchestrator import process_message, _is_explored, EXPLORATION_DIR
from app.database.connection import db
from app.database.schema_cache import schema_manager
from app.rag.indexer import rag_indexer
from app.llm.claude_client import llm_client

app = FastAPI(title="MySQL 智能查询助手")

# Session storage: {session_id: {"db_id": str, "history": list, "table_filter": list|None}}
sessions: dict = {}


# ---- Pydantic Models ----

class ProfileCreate(BaseModel):
    name: str
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""
    db_type: str = "mysql"
    description: str = ""


class ProfileUpdate(BaseModel):
    name: str = None
    host: str = None
    port: int = None
    user: str = None
    password: str = None
    database: str = None
    db_type: str = None
    description: str = None


# ---- Startup ----

def get_static_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "static")


@app.on_event("startup")
async def startup():
    """Initialize database connection, schema cache, and RAG index."""
    print("Starting MySQL 智能查询助手...")

    # Load profiles
    profiles = config.load_profiles()
    print(f"✓ 加载了 {len(profiles)} 个数据库配置")

    # Connect to default database
    try:
        default = profiles[0] if profiles else config.get_default_profile()
        db.connect_to(default)
        print(f"✓ 已连接到数据库: {default.name} ({default.host}:{default.port}/{default.database})")
    except Exception as e:
        print(f"✗ 数据库连接失败: {e}")
        print("  请在 .env 中配置数据库或通过 API 添加数据库")

    # Initialize schema cache for connected database
    try:
        if db.current_profile:
            cache = schema_manager.get_cache(db.current_profile.id)
            if not cache.load_from_disk():
                print("  构建初始 schema 缓存...")
                cache.ensure_fresh()
            snapshot = cache.snapshot
            if snapshot:
                print(f"✓ Schema 缓存就绪 ({len(snapshot.tables)} 张表)")
    except Exception as e:
        print(f"✗ Schema 缓存初始化失败: {e}")

    # Build RAG index in background (model download can be slow / unreachable)
    import threading
    def _build_rag():
        try:
            rag_indexer.build()
            mode = 'FAISS' if not rag_indexer._fallback else 'keyword'
            print(f"✓ RAG 索引就绪 ({mode} 模式)")
        except Exception:
            print("  RAG 索引不可用（非关键，将使用关键词搜索）")
    threading.Thread(target=_build_rag, daemon=True).start()
    print("  RAG 索引后台构建中...")

    # Check LLM
    if llm_client.available:
        print(f"✓ LLM 可用 (provider: {llm_client.provider}, model: {config.LLM_MODEL})")
    else:
        print(f"⚠ LLM 不可用 — 运行在受限模式")

    print(f"\nServer: http://localhost:{config.PORT}")


# ---- Frontend ----

@app.get("/", response_class=HTMLResponse)
async def index():
    static_dir = get_static_dir()
    html_path = os.path.join(static_dir, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


# ---- Health ----

@app.get("/api/health")
async def health():
    db_ok = False
    try:
        db.conn.ping()
        db_ok = True
    except Exception:
        pass

    snapshot = schema_manager.snapshot if hasattr(schema_manager, 'snapshot') else None
    return {
        "status": "ok",
        "database": db_ok,
        "db_name": db.current_db_name if db_ok else None,
        "llm_available": llm_client.available,
        "rag_ready": rag_indexer.ready,
        "schema_tables": len(snapshot.tables) if snapshot else 0,
    }


# ---- Database Profile Management ----

@app.get("/api/databases")
async def list_databases():
    """List all configured database profiles."""
    profiles = config.load_profiles()
    result = []
    for p in profiles:
        explored = _is_explored(p.id)
        result.append({
            **p.to_dict(),
            "explored": explored,
            "connected": db.current_profile and db.current_profile.id == p.id,
        })
    return {"profiles": result, "current": db.current_profile.id if db.current_profile else None}


@app.post("/api/databases")
async def add_database(profile: ProfileCreate):
    """Add a new database profile after testing the connection."""
    new_profile = DatabaseProfile(
        name=profile.name,
        host=profile.host,
        port=profile.port,
        user=profile.user,
        password=profile.password,
        database=profile.database,
        db_type=profile.db_type,
        description=profile.description,
    )

    # Test connection
    result = db.test_connection(new_profile)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    # Save
    profiles = config.load_profiles()
    profiles.append(new_profile)
    config.save_profiles(profiles)

    return {
        "success": True,
        "profile": new_profile.to_dict(),
        "message": result["message"],
    }


@app.put("/api/databases/{profile_id}")
async def update_database(profile_id: str, update: ProfileUpdate):
    """Update an existing database profile."""
    profiles = config.load_profiles()
    for i, p in enumerate(profiles):
        if p.id == profile_id:
            if update.name is not None:
                p.name = update.name
            if update.host is not None:
                p.host = update.host
            if update.port is not None:
                p.port = update.port
            if update.user is not None:
                p.user = update.user
            if update.password is not None:
                p.password = update.password
            if update.database is not None:
                p.database = update.database
            if update.db_type is not None:
                p.db_type = update.db_type
            if update.description is not None:
                p.description = update.description

            # Test updated connection
            result = db.test_connection(p)
            if not result["success"]:
                raise HTTPException(status_code=400, detail=result["message"])

            config.save_profiles(profiles)
            schema_manager.invalidate(profile_id)
            return {"success": True, "profile": p.to_dict(), "message": result["message"]}

    raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")


@app.delete("/api/databases/{profile_id}")
async def delete_database(profile_id: str):
    """Delete a database profile."""
    profiles = config.load_profiles()
    profiles = [p for p in profiles if p.id != profile_id]
    if len(profiles) == len(config.load_profiles()):
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")

    config.save_profiles(profiles)

    # Clean up exploration data
    path = os.path.join(EXPLORATION_DIR, f"{profile_id}.json")
    if os.path.exists(path):
        os.remove(path)

    return {"success": True}


# ---- Database Exploration ----

@app.get("/api/databases/{profile_id}/status")
async def database_status(profile_id: str, refresh: bool = False):
    """Get exploration status and schema info for a database.

    Args:
        refresh: If true, force a full schema refresh instead of using cache.
    """
    profiles = config.load_profiles()
    profile = next((p for p in profiles if p.id == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")

    explored = _is_explored(profile_id)

    # Try to get schema (even if not fully explored, try loading cache)
    snapshot = None
    try:
        db.connect_to(profile)
        if refresh:
            schema_manager.invalidate(profile_id)
        snapshot = schema_manager.ensure_fresh(profile_id)
    except Exception:
        pass

    result = {
        "profile_id": profile_id,
        "name": profile.name,
        "database": profile.database,
        "db_type": profile.db_type,
        "explored": explored,
    }

    # Parse agent-generated suggestions from exploration report
    suggestions = None
    if explored:
        path = os.path.join(EXPLORATION_DIR, f"{profile_id}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                exp_data = json.load(f)
            result["report"] = exp_data.get("report", "")[:500]
            result["explored_at"] = exp_data.get("explored_at", "")
            # Parse ```suggestions block from the agent's report
            import re
            report_text = exp_data.get("report", "")
            m = re.search(r'```suggestions\s*\n(.*?)```', report_text, re.DOTALL)
            if m:
                suggestions = [s.strip() for s in m.group(1).strip().split('\n') if s.strip()]
        except Exception:
            pass

    if snapshot:
        tables = list(snapshot.tables.keys())
        result["tables"] = tables
        result["table_count"] = len(tables)
        result["foreign_key_count"] = len(snapshot.foreign_keys)
        if snapshot.summary:
            result["total_rows"] = snapshot.summary.total_rows_estimate

    # Set suggestions: agent-generated first, then fallback
    if suggestions and len(suggestions) >= 2:
        result["suggestions"] = suggestions[:5]
    elif snapshot:
        tables = list(snapshot.tables.keys())
        result["suggestions"] = _fallback_suggestions(tables)
    else:
        result["suggestions"] = ["介绍一下当前数据库架构", "数据库有哪些表？"]

    return result


def _fallback_suggestions(tables: list) -> list:
    """Minimal fallback suggestions when no agent-generated ones exist."""
    suggestions = ["介绍一下当前数据库架构", "数据库有哪些表？"]
    for t in tables[:3]:
        suggestions.append(f"查询{t}表中有哪些数据")
    return suggestions[:5]


@app.post("/api/databases/{profile_id}/rebuild-cache")
async def rebuild_cache(profile_id: str, session_id: str = Query(default=None)):
    """Full cache rebuild — deletes cache files and rebuilds from scratch.

    If session_id is provided, uses that session's table_filter for the rebuild.
    """
    import os, glob

    profiles = config.load_profiles()
    profile = next((p for p in profiles if p.id == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")

    # Look up table filter from session and set globally
    table_filter = None
    if session_id and session_id in sessions:
        table_filter = sessions[session_id].get("table_filter")
    from app.database.schema_cache import set_active_table_filter
    set_active_table_filter(table_filter)

    try:
        db.connect_to(profile)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连接失败: {e}")

    # Delete all cache files for this profile
    cache_files = [
        f"data/schema_{profile_id}.json",
        f"data/schema_{profile_id}_detailed.json",
        f"data/explorations/{profile_id}.json",
    ]
    for cf in cache_files:
        try:
            os.remove(cf)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[Rebuild] Warning: could not delete {cf}: {e}")

    # Also clean up any stale detailed cache files
    detailed_dir = "data/detailed_cache"
    if os.path.isdir(detailed_dir):
        for f in glob.glob(os.path.join(detailed_dir, f"{profile_id}*")):
            try:
                os.remove(f)
            except Exception:
                pass

    # Invalidate in-memory cache
    schema_manager.invalidate(profile_id)

    # Rebuild with table filter if set
    try:
        snapshot = schema_manager.ensure_fresh(profile_id, table_filter)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"缓存重建失败: {e}")

    n_tables = len(snapshot.tables) if snapshot else 0
    total_tables = n_tables  # For display
    filter_note = f"（筛选 {n_tables} 张" if table_filter else f"（全部 {n_tables} 张"

    # Also trigger re-exploration if not already explored
    from app.agent.orchestrator import _is_explored
    if _is_explored(profile_id):
        exp_path = f"data/explorations/{profile_id}.json"
        try:
            os.remove(exp_path)
        except Exception:
            pass

    # Check background explorer progress
    from app.agent.background_explorer import background_explorer
    bg_progress = background_explorer.get_progress(profile_id)

    return {
        "success": True,
        "profile_id": profile_id,
        "name": profile.name,
        "table_count": n_tables,
        "message": f"缓存已重建{filter_note} 张表）",
        "background_progress": bg_progress,
    }


@app.post("/api/databases/{profile_id}/refresh")
async def refresh_connection(profile_id: str):
    """Refresh database connection and schema cache (lightweight — no full rebuild)."""
    profiles = config.load_profiles()
    profile = next((p for p in profiles if p.id == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")

    try:
        db.connect_to(profile)
        # Invalidate cache to force freshness check on next query
        schema_manager.invalidate(profile_id)
        snapshot = schema_manager.ensure_fresh(profile_id)
        n_tables = len(snapshot.tables) if snapshot else 0
        return {
            "success": True,
            "profile_id": profile_id,
            "name": profile.name,
            "table_count": n_tables,
            "message": f"连接已刷新，缓存就绪（{n_tables} 张表）",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新失败: {e}")


@app.post("/api/databases/{profile_id}/explore")
async def trigger_explore(profile_id: str):
    """Trigger database exploration (SSE stream)."""
    profiles = config.load_profiles()
    profile = next((p for p in profiles if p.id == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")

    async def explore_stream():
        async for event in explore_database(profile):
            yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        explore_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---- Table Filter ----

@app.get("/api/tables")
async def list_tables(db_id: str = Query(default=None, description="Database profile ID")):
    """List ALL tables directly from the database (ignores cache filter).

    Always returns the full table list so the filter UI shows every table,
    even when a filter is active and the cache only contains selected tables.
    """
    try:
        if db_id:
            profiles = config.load_profiles()
            profile = next((p for p in profiles if p.id == db_id), None)
            if profile:
                db.connect_to(profile)
        if not db.current_profile:
            return {"tables": [], "message": "未连接数据库"}

        bt = db.db_type
        is_dm = bt in ("dameng", "dm", "dm8")
        is_pg = bt == "postgresql"

        with db.cursor() as cur:
            if is_dm:
                cur.execute(
                    "SELECT TABLE_NAME, NUM_ROWS AS TABLE_ROWS, "
                    "COALESCE((SELECT COMMENTS FROM ALL_TAB_COMMENTS "
                    "WHERE TABLE_NAME = t.TABLE_NAME AND OWNER = t.OWNER), '') AS TABLE_COMMENT "
                    "FROM ALL_TABLES t "
                    "WHERE TABLE_NAME NOT LIKE '##%' AND TABLE_NAME NOT LIKE 'SREF%' "
                    "AND OWNER = USER ORDER BY TABLE_NAME"
                )
            elif is_pg:
                cur.execute(
                    "SELECT tablename AS TABLE_NAME, COALESCE(n_live_tup, 0) AS TABLE_ROWS, "
                    "COALESCE(obj_description(pc.oid), '') AS TABLE_COMMENT "
                    "FROM pg_catalog.pg_tables t "
                    "LEFT JOIN pg_catalog.pg_class pc ON pc.relname = t.tablename "
                    "LEFT JOIN pg_catalog.pg_stat_user_tables s ON s.relname = t.tablename "
                    "AND s.schemaname = t.schemaname "
                    "WHERE t.schemaname NOT IN ('pg_catalog', 'information_schema') "
                    "ORDER BY t.tablename"
                )
            else:
                cur.execute(
                    "SELECT TABLE_NAME, TABLE_ROWS, TABLE_COMMENT "
                    "FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
                    "ORDER BY TABLE_NAME",
                    (profile.database if profile else db.current_db_name,),
                )
            rows = cur.fetchall()

        # Also get column counts AND row counts from cache (cache has Dameng COUNT(*) fix)
        cache = schema_manager.get_cache(db.current_profile.id)
        col_counts = {}
        row_counts = {}
        if cache.snapshot:
            for tname, meta in cache.snapshot.tables.items():
                col_counts[tname] = len(meta.columns)
                row_counts[tname] = meta.row_count

        tables = []
        for r in rows:
            tname = r["TABLE_NAME"]
            raw_rows = r.get("TABLE_ROWS", 0) or 0
            # For Dameng, prefer cache row count (has COUNT(*) fallback) over ALL_TABLES.NUM_ROWS
            cached_rows = row_counts.get(tname)
            if is_dm and cached_rows is not None and cached_rows > 0:
                display_rows = cached_rows
            elif cached_rows is not None:
                display_rows = cached_rows
            else:
                display_rows = raw_rows
            tables.append({
                "name": tname,
                "row_count": display_rows,
                "column_count": col_counts.get(tname, 0),
                "comment": r.get("TABLE_COMMENT", "") or "",
            })
        return {"tables": tables}
    except Exception as e:
        return {"tables": [], "message": str(e)}


@app.post("/api/session/{session_id}/table-filter")
async def set_table_filter(session_id: str, data: dict):
    """Set the table filter for a session. Pass {"tables": ["table1","table2"]} or {"tables": null} for all."""
    if session_id not in sessions:
        sessions[session_id] = {"db_id": None, "history": [], "table_filter": None}
    tables = data.get("tables")
    sessions[session_id]["table_filter"] = tables if tables else None
    n = len(tables) if tables else 0
    return {"success": True, "message": f"已选择 {n} 张表" if tables else "已清除表筛选，使用全部表",
            "table_filter": sessions[session_id]["table_filter"]}


@app.get("/api/session/{session_id}/table-filter")
async def get_table_filter(session_id: str):
    """Get the current table filter for a session."""
    if session_id not in sessions:
        return {"table_filter": None}
    return {"table_filter": sessions[session_id].get("table_filter")}


# ---- Admin Q&A ----

@app.get("/api/admin/questions")
async def list_admin_questions(
    status: str = Query(default=None, description="Filter: pending, answered, ignored, or all"),
    profile_id: str = Query(default=None, description="Database profile ID. Uses current connection if omitted."),
):
    """List admin clarification questions, scoped by profile_id.

    Query params:
        status: Filter by status. 'pending' = unanswered, 'answered' = answered,
                'ignored' = dismissed. Omit for all.
        profile_id: Database profile to query. Defaults to currently connected profile.
    """
    from app.rag.admin_clarifications import clarification_store

    # Determine profile_id
    if not profile_id:
        profile_id = db.current_profile.id if db.current_profile else "default"

    if status == "pending":
        entries = clarification_store.get_pending(profile_id)
    elif status == "answered":
        entries = clarification_store.get_all_answered(profile_id)
    else:
        entries = clarification_store.get_all(profile_id)

    return {
        "questions": [
            {
                "question_id": e.question_id,
                "table_name": e.table_name,
                "column_name": e.column_name,
                "question": e.question,
                "sample_values": e.sample_values,
                "answer": e.answer,
                "status": e.status,
                "created_at": e.created_at,
                "answered_at": e.answered_at,
                "profile_id": e.profile_id,
            }
            for e in entries
        ],
        "pending_count": clarification_store.get_pending_count(profile_id),
        "total_count": len(entries),
        "profile_id": profile_id,
    }


@app.post("/api/admin/questions/{question_id}/answer")
async def answer_admin_question(question_id: str, data: dict):
    """Answer a pending admin clarification question.

    Request body: {"answer": "管理员回答内容", "profile_id": "optional_profile_id"}
    """
    from app.rag.admin_clarifications import clarification_store

    answer = data.get("answer", "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="答案不能为空")

    # Determine profile_id
    profile_id = data.get("profile_id") or (db.current_profile.id if db.current_profile else "default")

    if not clarification_store.answer_question(profile_id, question_id, answer):
        raise HTTPException(status_code=404, detail=f"问题 {question_id} 未找到或已回答")

    # Trigger RAG re-index with the new admin clarification
    admin_doc = clarification_store.build_rag_document(profile_id)
    rag_indexer.build_admin_index(admin_doc, profile_id)

    return {"success": True, "question_id": question_id, "message": "答案已保存并索引到知识库"}


@app.post("/api/admin/questions/{question_id}/ignore")
async def ignore_admin_question(question_id: str, data: dict = None):
    """Dismiss a pending admin clarification question without answering.

    Optional request body: {"profile_id": "optional_profile_id"}
    """
    from app.rag.admin_clarifications import clarification_store

    profile_id = (data or {}).get("profile_id") or (db.current_profile.id if db.current_profile else "default")

    if not clarification_store.ignore_question(profile_id, question_id):
        raise HTTPException(status_code=404, detail=f"问题 {question_id} 未找到")

    return {"success": True, "message": "问题已忽略"}


@app.get("/api/admin/explore-database")
async def explore_database_proactive(
    db_id: str = Query(default=None, description="Database profile ID"),
    session_id: str = Query(default=None, description="Session ID for table_filter"),
):
    """Trigger proactive database exploration via SSE.

    The agent systematically scans all tables, identifies columns with
    encoded/abbreviated values, and asks the admin about them.
    Progress is streamed via SSE events.
    """
    from app.agent.orchestrator import run_proactive_exploration

    # Resolve database profile
    profile = None
    if db_id:
        profiles = config.load_profiles()
        for p in profiles:
            if p.id == db_id:
                profile = p
                break
    if not profile:
        try:
            profile = config.load_profiles()[0]
        except Exception:
            pass

    # Get table filter from session if available
    table_filter = None
    if session_id and session_id in sessions:
        table_filter = sessions[session_id].get("table_filter")

    async def event_generator():
        import asyncio

        try:
            stream = run_proactive_exploration(profile, table_filter)
            while True:
                try:
                    event = await asyncio.wait_for(stream.__anext__(), timeout=300.0)
                    # Map reserved "error" SSE event type to "explore_error"
                    sse_event_type = "explore_error" if event['type'] == "error" else event['type']
                    yield f"event: {sse_event_type}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield f"event: explore_error\ndata: {json.dumps({'type': 'error', 'message': '探索超时', 'message_id': 'explore'}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'type': 'done', 'status': 'error', 'message': '探索超时'}, ensure_ascii=False)}\n\n"
                    break
        except Exception as e:
            yield f"event: explore_error\ndata: {json.dumps({'type': 'error', 'message': str(e), 'message_id': 'explore'}, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'type': 'done', 'status': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---- Chat ----

@app.get("/chat/stream")
async def chat_stream(
    message: str = Query(..., description="User message"),
    session_id: str = Query(default=None, description="Session ID"),
    db_id: str = Query(default=None, description="Database profile ID"),
    table_filter: str = Query(default=None, description="Comma-separated table filter"),
):
    """SSE streaming endpoint for chat messages."""
    if not message or not message.strip():
        return StreamingResponse(
            _single_event({"type": "error", "message": "Empty message"}),
            media_type="text/event-stream",
        )

    if len(message) > config.MAX_MESSAGE_LENGTH:
        return StreamingResponse(
            _single_event({"type": "error", "message": f"Message too long (max {config.MAX_MESSAGE_LENGTH} chars)"}),
            media_type="text/event-stream",
        )

    # Parse table_filter from query param (comma-separated, from frontend localStorage)
    filter_from_param = None
    if table_filter:
        filter_from_param = [t.strip() for t in table_filter.split(",") if t.strip()]

    # Get or create session
    sid = session_id or str(uuid.uuid4())[:8]
    if sid not in sessions:
        sessions[sid] = {"db_id": db_id, "history": [], "table_filter": filter_from_param}

    session = sessions[sid]

    # If db_id changed, switch database and reset history + filter
    if db_id and session["db_id"] != db_id:
        session["db_id"] = db_id
        session["history"] = []
        session["table_filter"] = filter_from_param

    # Query param filter always overrides (most current)
    if filter_from_param is not None:
        session["table_filter"] = filter_from_param

    history = session["history"]

    # Resolve database profile
    db_profile = None
    if db_id:
        profiles = config.load_profiles()
        db_profile = next((p for p in profiles if p.id == db_id), None)
        if not db_profile:
            return StreamingResponse(
                _single_event({"type": "error", "message": f"Database profile {db_id} not found"}),
                media_type="text/event-stream",
            )

    async def event_generator():
        import asyncio
        message_id = str(uuid.uuid4())[:8]

        try:
            stream = process_message(message, history, db_profile, session.get("table_filter"))
            while True:
                try:
                    event = await asyncio.wait_for(stream.__anext__(), timeout=180.0)
                    event["message_id"] = message_id
                    yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield f"event: error\ndata: {json.dumps({'type': 'error', 'message': '请求处理超时，请重试', 'message_id': message_id}, ensure_ascii=False)}\n\n"
                    break

            # Cleanup old sessions
            if len(sessions) > 100:
                oldest = min(sessions.keys(), key=lambda k: len(sessions[k]["history"]) if sessions[k]["history"] else 0)
                del sessions[oldest]

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'message': str(e), 'message_id': message_id}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _single_event(event: dict) -> str:
    yield f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


# ---- Static Files ----

static_dir = get_static_dir()
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
