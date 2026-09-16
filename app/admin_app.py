"""Admin panel FastAPI application — runs on a separate port (8001).

This is an independent app from the main chat app (port 8000). It provides
a standalone admin interface for managing database clarification questions,
exploring databases, and reviewing answered clarifications — all scoped
per database profile.
"""

import json
import os
import uuid

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import config, DatabaseProfile
from app.agent.orchestrator import _is_explored, EXPLORATION_DIR, run_proactive_exploration
from app.database.connection import db
from app.database.schema_cache import schema_manager
from app.rag.indexer import rag_indexer

admin_app = FastAPI(title="MySQL 智能查询助手 - 管理面板")


# ---- Pydantic Models ----

class AnswerRequest(BaseModel):
    answer: str
    profile_id: str = ""


class IgnoreRequest(BaseModel):
    profile_id: str = ""


# ---- Startup ----

def _get_static_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "static")


@admin_app.on_event("startup")
async def admin_startup():
    """Initialize the admin panel — no auto-connect to any DB."""
    print("Starting Admin Panel on port 8001...")

    # Load profiles
    profiles = config.load_profiles()
    print(f"✓ 加载了 {len(profiles)} 个数据库配置")

    # Don't auto-connect — admin selects database manually

    # Build RAG index in background
    import threading
    def _build_rag():
        try:
            rag_indexer.build()
            mode = 'FAISS' if not rag_indexer._fallback else 'keyword'
            print(f"✓ RAG 索引就绪 ({mode} 模式)")
        except Exception:
            print("  RAG 索引不可用（非关键）")
    threading.Thread(target=_build_rag, daemon=True).start()

    print(f"Admin Panel: http://localhost:8001")


# ---- Frontend ----

@admin_app.get("/", response_class=HTMLResponse)
async def admin_index():
    static_dir = _get_static_dir()
    html_path = os.path.join(static_dir, "admin.html")
    if not os.path.exists(html_path):
        return HTMLResponse("<h1>管理面板未配置</h1><p>admin.html 未找到</p>", status_code=404)
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


# ---- Health ----

@admin_app.get("/api/admin/health")
async def admin_health():
    return {
        "status": "ok",
        "rag_ready": rag_indexer.ready,
    }


# ---- Profile Management ----

@admin_app.get("/api/admin/profiles")
async def list_admin_profiles():
    """List all database profiles with admin-relevant status."""
    from app.rag.admin_clarifications import clarification_store

    profiles = config.load_profiles()
    result = []
    for p in profiles:
        # Get schema info from cache (no DB connection needed)
        table_count = 0
        try:
            cache = schema_manager.get_cache(p.id)
            if cache.snapshot:
                table_count = len(cache.snapshot.tables)
        except Exception:
            pass

        explored = _is_explored(p.id)
        pending_count = clarification_store.get_pending_count(p.id)

        result.append({
            "id": p.id,
            "name": p.name,
            "database": p.database,
            "db_type": p.db_type,
            "host": p.host,
            "port": p.port,
            "description": p.description,
            "table_count": table_count,
            "explored": explored,
            "pending_count": pending_count,
        })

    return {"profiles": result}


@admin_app.get("/api/admin/profile/{profile_id}")
async def admin_profile_detail(profile_id: str):
    """Get detailed info for a single profile, connecting to the DB if needed."""
    from app.rag.admin_clarifications import clarification_store

    profiles = config.load_profiles()
    profile = next((p for p in profiles if p.id == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")

    # Try to connect and get schema
    table_count = 0
    tables_list = []
    try:
        db.connect_to(profile)
        snapshot = schema_manager.ensure_fresh(profile_id)
        if snapshot:
            table_count = len(snapshot.tables)
            tables_list = sorted(snapshot.tables.keys())
    except Exception as e:
        # Try loading from cache
        try:
            cache = schema_manager.get_cache(profile_id)
            if cache.snapshot:
                table_count = len(cache.snapshot.tables)
                tables_list = sorted(cache.snapshot.tables.keys())
        except Exception:
            pass

    explored = _is_explored(profile_id)
    pending_count = clarification_store.get_pending_count(profile_id)

    # Get exploration progress
    progress = None
    try:
        snapshot = schema_manager.get_cache(profile_id).snapshot
        if snapshot:
            progress = clarification_store.get_exploration_progress(
                profile_id,
                {tname: len(meta.columns) for tname, meta in snapshot.tables.items()}
            )
    except Exception:
        pass

    return {
        "id": profile_id,
        "name": profile.name,
        "database": profile.database,
        "db_type": profile.db_type,
        "host": profile.host,
        "port": profile.port,
        "description": profile.description,
        "table_count": table_count,
        "tables": tables_list,
        "explored": explored,
        "pending_count": pending_count,
        "exploration_progress": progress,
    }


# ---- Admin Questions (per-profile) ----

@admin_app.get("/api/admin/questions")
async def list_admin_questions(
    profile_id: str = Query(..., description="Database profile ID (required)"),
    status: str = Query(default=None, description="Filter: pending, answered, or all"),
):
    """List admin clarification questions for a specific profile."""
    from app.rag.admin_clarifications import clarification_store

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


@admin_app.post("/api/admin/questions/{question_id}/answer")
async def answer_admin_question(question_id: str, data: AnswerRequest):
    """Answer a pending admin clarification question.

    Request body: {"answer": "...", "profile_id": "..."}
    """
    from app.rag.admin_clarifications import clarification_store

    answer = data.answer.strip()
    if not answer:
        raise HTTPException(status_code=400, detail="答案不能为空")
    if not data.profile_id:
        raise HTTPException(status_code=400, detail="缺少 profile_id")

    if not clarification_store.answer_question(data.profile_id, question_id, answer):
        raise HTTPException(status_code=404, detail=f"问题 {question_id} 未找到或已回答")

    # Rebuild RAG index for this profile
    admin_doc = clarification_store.build_rag_document(data.profile_id)
    rag_indexer.build_admin_index(admin_doc, data.profile_id)

    return {"success": True, "question_id": question_id, "message": "答案已保存并索引到知识库"}


@admin_app.post("/api/admin/questions/{question_id}/ignore")
async def ignore_admin_question(question_id: str, data: IgnoreRequest = None):
    """Dismiss a pending admin clarification question without answering."""
    from app.rag.admin_clarifications import clarification_store

    profile_id = data.profile_id if data and data.profile_id else ""
    if not profile_id:
        raise HTTPException(status_code=400, detail="缺少 profile_id")

    if not clarification_store.ignore_question(profile_id, question_id):
        raise HTTPException(status_code=404, detail=f"问题 {question_id} 未找到")

    return {"success": True, "message": "问题已忽略"}


# ---- Exploration ----

@admin_app.get("/api/admin/explore-database")
async def explore_database_proactive(
    profile_id: str = Query(..., description="Database profile ID (required)"),
):
    """Trigger proactive database exploration via SSE for a specific profile.

    The agent systematically scans all tables, identifies columns with
    encoded/abbreviated values, and asks the admin about them.
    Progress is streamed via SSE events.
    """
    profiles = config.load_profiles()
    profile = None
    for p in profiles:
        if p.id == profile_id:
            profile = p
            break
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")

    async def event_generator():
        import asyncio

        try:
            stream = run_proactive_exploration(profile, None)
            while True:
                try:
                    event = await asyncio.wait_for(stream.__anext__(), timeout=300.0)
                    # Map reserved "error" SSE event type to "explore_error"
                    # because browsers treat event:error as transport-level, not named
                    sse_event_type = "explore_error" if event['type'] == "error" else event['type']
                    yield f"event: {sse_event_type}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield f"event: explore_error\ndata: {json.dumps({'type': 'error', 'message': '探索超时'}, ensure_ascii=False)}\n\n"
                    yield f"event: done\ndata: {json.dumps({'type': 'done', 'status': 'error', 'message': '探索超时'}, ensure_ascii=False)}\n\n"
                    break
        except Exception as e:
            yield f"event: explore_error\ndata: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
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


# ---- Cache Management ----

@admin_app.post("/api/admin/profile/{profile_id}/rebuild-cache")
async def admin_rebuild_cache(profile_id: str):
    """Rebuild schema cache for a profile."""
    profiles = config.load_profiles()
    profile = next((p for p in profiles if p.id == profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile {profile_id} not found")

    try:
        db.connect_to(profile)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连接失败: {e}")

    # Clean up cache files
    import glob
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

    detailed_dir = "data/detailed_cache"
    if os.path.isdir(detailed_dir):
        for f in glob.glob(os.path.join(detailed_dir, f"{profile_id}*")):
            try:
                os.remove(f)
            except Exception:
                pass

    schema_manager.invalidate(profile_id)

    try:
        snapshot = schema_manager.ensure_fresh(profile_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"缓存重建失败: {e}")

    n_tables = len(snapshot.tables) if snapshot else 0
    return {
        "success": True,
        "profile_id": profile_id,
        "table_count": n_tables,
        "message": f"缓存已重建（{n_tables} 张表）",
    }


# ---- Static Files ----

static_dir = _get_static_dir()
if os.path.isdir(static_dir):
    admin_app.mount("/static", StaticFiles(directory=static_dir), name="static")
