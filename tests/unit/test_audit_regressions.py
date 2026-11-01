"""Regression tests for the October security and correctness audit.

No real database, model API, credentials or project runtime state are used.
"""

import asyncio
import socket
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from dbchat.app import create_app
from dbchat.config import Settings
from dbchat.db.dialects import get_dialect
from dbchat.db.executor import SqlExecutor
from dbchat.db.guard import inspect
from dbchat.di import build_context, ensure_conversation
from dbchat.domain.profile import DatabaseProfile, DialectKind
from dbchat.domain.schema import CatalogScope, SchemaSnapshot, TableMeta
from dbchat.rag.indexer import IndexState, RAGIndexer
from dbchat.storage import Storage
from dbchat.tools import build_registry
from dbchat.tools.docs import SearchDocsParams, search_documentation
from dbchat.tools.sandbox import run_python_code
from dbchat.usecases.chat import build_chat_use_case


@pytest.fixture
def context(tmp_path):
    settings = Settings(
        _env_file=None,
        var_dir=tmp_path,
        web_dist_dir=tmp_path / "dist",
        auth_admin_token="a" * 32,
        auth_user_token="u" * 32,
    )
    storage = Storage(settings)
    storage.migrate()
    connections = SimpleNamespace(
        close=AsyncMock(), close_all=AsyncMock(), connected_profile_ids=lambda: set()
    )
    ctx = build_context(
        settings,
        storage=storage,
        connections=connections,
        rag=RAGIndexer(settings, doc_paths=[]),
        schema_store=None,
        tools=build_registry(settings),
        sandbox=None,
    )
    ctx.profiles.upsert(DatabaseProfile(id="default", name="A", database="audit_a"))
    ctx.profiles.upsert(DatabaseProfile(id="other", name="B", database="audit_b"))
    yield ctx
    storage.close()


async def test_python_sandbox_blocks_outside_reads(tmp_path):
    marker = tmp_path / "private-marker.txt"
    marker.write_text("synthetic-audit-marker")
    result = await run_python_code(
        "result = open.__globals__['_builtins'].open(input_data['marker']).read()",
        input_data={"marker": str(marker)},
        artifacts_dir=tmp_path / "artifacts",
        timeout_s=5,
    )
    assert not result.ok
    assert marker.read_text() == "synthetic-audit-marker"


async def test_unauthenticated_profile_delete(context):
    app = create_app(context.settings, context=context, build_rag=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://audit"
    ) as client:
        response = await client.delete("/api/databases/other")
    assert response.status_code == 401
    assert context.profiles.get("other") is not None


@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_table_filter_preserves_profile_and_history(context, method):
    await ensure_conversation(
        context, session_id="audit-session", profile_id="other", table_filter=["orders"]
    )
    await context.conversations.astore_message("audit-session", "user", "synthetic-history")
    before = await context.conversations.aget("audit-session")
    assert before.profile_id == "other"
    app = create_app(context.settings, context=context, build_rag=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://audit", auth=("user", "u" * 32)
    ) as client:
        kwargs = {"json": {"tables": ["orders"]}} if method == "POST" else {}
        response = await client.request(method, "/api/session/audit-session/table-filter", **kwargs)
    after = await context.conversations.aget("audit-session")
    history = await context.conversations.ahistory("audit-session")
    assert response.status_code == 200
    assert after.profile_id == "other"
    assert history == [{"role": "user", "content": "synthetic-history"}]


@pytest.mark.parametrize(
    "kind,sql",
    [
        (DialectKind.POSTGRESQL, "SELECT setval('employee_id_seq', 100)"),
        (DialectKind.POSTGRESQL, "SELECT 1 AS id INTO TEMP employee"),
        (DialectKind.MYSQL, "SELECT LOAD_FILE('/tmp/synthetic-audit-marker')"),
    ],
)
def test_read_only_guard_rejects_side_effects(kind, sql):
    snapshot = SchemaSnapshot(db_name="audit", tables={"employee": TableMeta(name="employee")})
    outcome = inspect(get_dialect(kind), sql, snapshot=snapshot, row_limit=201)
    assert not outcome.ok


def test_nested_cte_cannot_bypass_table_scope():
    query = "SELECT * FROM secret WHERE EXISTS (WITH secret AS (SELECT 1) SELECT * FROM secret)"
    outcome = inspect(
        get_dialect(DialectKind.POSTGRESQL),
        query,
        scope=CatalogScope(frozenset({"allowed"})),
        row_limit=201,
    )
    assert not outcome.ok


def test_cross_database_name_is_rejected():
    snapshot = SchemaSnapshot(db_name="audit", tables={"employee": TableMeta(name="employee")})
    outcome = inspect(
        get_dialect(DialectKind.MYSQL),
        "SELECT * FROM other_db.employee",
        snapshot=snapshot,
        scope=CatalogScope(frozenset({"employee"})),
        row_limit=201,
    )
    assert not outcome.ok


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM employee LIMIT 100000000",
        "SELECT COUNT(*) OVER () AS total, employee.* FROM employee",
    ],
)
async def test_row_cap_bounds_sql_and_cursor_fetch(sql):
    calls = []

    class Cursor:
        description = [("id",)]

        def execute(self, query):
            calls.append(query)

        def column_names(self):
            return ["id"]

        def fetchmany(self, size):
            calls.append(size)
            return [{"id": i} for i in range(size)]

        def fetchall(self):
            raise AssertionError("must not fetch unbounded rows")

    @contextmanager
    def cursor():
        yield Cursor()

    settings = Settings(_env_file=None, query_row_limit=200)
    lease = SimpleNamespace(
        dialect=get_dialect(DialectKind.MYSQL),
        profile=DatabaseProfile(id="audit", database="audit"),
        timeout_ms=settings.db_query_timeout_s * 1000,
        cursor=cursor,
        connection=None,
    )
    result = await SqlExecutor(lease, settings).query(sql)
    assert "LIMIT 201" in calls[0]
    assert calls[1] == 201
    assert result.row_count == 200 and result.truncated


async def test_queries_use_ready_application_rag(context, monkeypatch):
    import dbchat.rag.indexer as indexer_module

    context.rag._swap(
        IndexState(chunks=("AUDIT_RAG_MARKER policy explanation",), ready=True, fallback=True)
    )
    monkeypatch.setattr(indexer_module, "_indexer", RAGIndexer(context.settings, doc_paths=[]))
    assert context.rag.search("AUDIT_RAG_MARKER")
    pipeline = build_chat_use_case(context)
    retrieved = await pipeline._rag_context("AUDIT_RAG_MARKER")
    tool_result = await search_documentation(
        SearchDocsParams(query="AUDIT_RAG_MARKER"),
        SimpleNamespace(rag=context.rag, profile_id="default"),
    )
    assert "AUDIT_RAG_MARKER" in retrieved
    assert tool_result.payload["matches"] == 1


@pytest.mark.parametrize("stage", ["computing", "visualizing"])
async def test_failed_agent_propagates_failure(context, monkeypatch, stage):
    import dbchat.usecases.chat as chat
    from dbchat.agent import AgentRun
    from dbchat.events.agent import AgentFailed
    from dbchat.events.chat import StageFinished
    from dbchat.events.project import RunView
    from dbchat.usecases.contracts import RunContext

    plan = SimpleNamespace(
        needs_computation=stage == "computing",
        strategy="audit",
        needs_chart=stage == "visualizing",
        chart_strategy="audit",
    )
    monkeypatch.setattr(chat, "plan_computation", AsyncMock(return_value=plan))

    async def failed_loop(*args, **kwargs):
        yield AgentFailed(agent="audit", code="llm_failed", message="synthetic failure")

    monkeypatch.setattr(chat, "run_agent_loop", failed_loop)
    pipeline = build_chat_use_case(context)
    run_ctx = RunContext(
        run_id="audit",
        profile=context.profiles.get("default"),
        scope=CatalogScope(),
        conversation_id="audit",
    )
    run = AgentRun(ctx=run_ctx, llm=context.llm, registry=context.tools, settings=context.settings)
    from dbchat.domain.errors import DbChatError

    events = []
    with pytest.raises(DbChatError, match="synthetic failure"):
        async for event in pipeline._compute_and_visualize(
            RunView(run_id="audit"), run_ctx, run, "audit"
        ):
            events.append(event)
    finished = [e for e in events if isinstance(e, StageFinished)]
    assert len(finished) == 1
    assert finished[0].ok is False
    assert run.state.get(chat.STATE_AGENT_ERROR) is None


@pytest.mark.parametrize(
    "credentials,expected",
    [
        (None, 401),
        (("admin", "wrong"), 401),
        (("user", "u" * 32), 403),
        (("admin", "a" * 32), 200),
    ],
)
async def test_admin_mutation_requires_admin(context, credentials, expected):
    app = create_app(context.settings, context=context, build_rag=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://audit", auth=credentials
    ) as client:
        response = await client.delete("/api/databases/other")
    assert response.status_code == expected
    assert (context.profiles.get("other") is None) == (expected == 200)


async def test_bearer_and_browser_auth_and_csrf(context):
    app = create_app(context.settings, context=context, build_rag=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://audit"
    ) as client:
        assert (
            await client.get("/api/databases", headers={"Authorization": "Bearer " + "u" * 32})
        ).status_code == 200
        assert (
            await client.get("/api/admin/questions", auth=("user", "u" * 32))
        ).status_code == 403
        assert (
            await client.delete(
                "/api/databases/other",
                auth=("admin", "a" * 32),
                headers={"Origin": "https://evil.invalid"},
            )
        ).status_code == 403
        assert (
            await client.get("/api/databases/default/status?refresh=true", auth=("user", "u" * 32))
        ).status_code == 403
        assert (
            await client.get("/api/databases", headers={"Authorization": "Basic %%%%"})
        ).status_code == 401
        context.settings.auth_admin_token = ""
        assert (await client.get("/api/databases")).status_code == 503


async def test_reading_unknown_filter_does_not_create_session(context):
    app = create_app(context.settings, context=context, build_rag=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://audit", auth=("user", "u" * 32)
    ) as client:
        assert (await client.get("/api/session/unknown/table-filter")).status_code == 404
    assert await context.conversations.aget("unknown") is None


async def test_admin_rag_is_profile_scoped_and_empty_rebuild_removes_answers(context):
    await context.rag.abuild_admin_index("AUDIT_SHARED_MARKER alpha-secret", "default")
    await context.rag.abuild_admin_index("AUDIT_SHARED_MARKER beta-secret", "other")
    pipeline = build_chat_use_case(context)
    alpha = await pipeline._rag_context("AUDIT_SHARED_MARKER", "default")
    beta = await pipeline._rag_context("AUDIT_SHARED_MARKER", "other")
    assert "alpha-secret" in alpha and "beta-secret" not in alpha
    assert "beta-secret" in beta and "alpha-secret" not in beta
    assert not await pipeline._rag_context("AUDIT_SHARED_MARKER")
    await context.rag.abuild_admin_index("", "default")
    assert not await pipeline._rag_context("AUDIT_SHARED_MARKER", "default")
    assert "beta-secret" in await pipeline._rag_context("AUDIT_SHARED_MARKER", "other")


async def test_sandbox_cannot_reach_host_network(tmp_path):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        result = await run_python_code(
            "s = open.__globals__['_builtins'].__import__('socket').socket()\n"
            "s.settimeout(0.2)\nresult = s.connect_ex(('127.0.0.1', input_data['port']))",
            input_data={"port": server.getsockname()[1]},
            artifacts_dir=tmp_path,
            timeout_s=5,
        )
    assert result.ok, result.error_detail
    assert int(result.result_text) != 0


async def test_sandbox_fails_closed_without_bwrap(tmp_path, monkeypatch):
    monkeypatch.setattr("dbchat.tools.sandbox.shutil.which", lambda name: None)
    result = await run_python_code("result = 1", artifacts_dir=tmp_path, timeout_s=2)
    assert not result.ok
    assert "bubblewrap" in result.error


def test_sandbox_rejects_symlink_outputs(tmp_path):
    from dbchat.tools.sandbox import _collect_artifacts, _read_result

    private = tmp_path / "private.json"
    private.write_text('{"ok":true}')
    work = tmp_path / "work"
    work.mkdir()
    (work / "_result.json").symlink_to(private)
    (work / "leak.png").symlink_to(private)
    assert _read_result(work / "_result.json") is None
    assert _collect_artifacts(work, tmp_path / "artifacts") == ()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT public.unknown_udf()",
        "SELECT nextval('s')",
        "SELECT * FROM employee FOR UPDATE",
        "EXPLAIN ANALYZE SELECT setval('s', 1)",
        "SELECT * FROM pg_catalog.pg_authid",
        "SELECT * FROM employee WHERE id IN (",
    ],
)
def test_unsafe_or_unparseable_sql_fails_closed(sql):
    assert not inspect(get_dialect(DialectKind.POSTGRESQL), sql, row_limit=200).ok


@pytest.mark.parametrize(
    "kind,sql,limit_text",
    [
        (DialectKind.POSTGRESQL, "SELECT * FROM employee OFFSET 10", "LIMIT 201"),
        (DialectKind.MYSQL, "SELECT * FROM employee WHERE rownum <= 5", "LIMIT 201"),
        (DialectKind.DAMENG, "SELECT * FROM employee WHERE ROWNUM <= 5", "FETCH FIRST 201"),
        (
            DialectKind.DAMENG,
            "SELECT * FROM employee FETCH FIRST 999999 ROWS ONLY",
            "FETCH FIRST 201",
        ),
        (
            DialectKind.MYSQL,
            "SELECT * FROM employee WHERE EXISTS (SELECT COUNT(*) FROM employee)",
            "LIMIT 201",
        ),
    ],
)
def test_limits_cover_offset_fetch_and_nested_aggregates(kind, sql, limit_text):
    result = inspect(get_dialect(kind), sql, row_limit=201)
    assert result.ok, result.errors
    assert limit_text in result.sql


@pytest.mark.parametrize("table", ["pg_tables", "all_tables", "public.pg_tables"])
def test_metadata_names_do_not_bypass_mysql_table_scope(table):
    result = inspect(
        get_dialect(DialectKind.MYSQL),
        f"SELECT * FROM {table}",
        scope=CatalogScope(frozenset({"employee"})),
        allowed_schemas=frozenset({"public"}),
        row_limit=201,
    )
    assert not result.ok


async def test_sandbox_cancellation_reaps_process_and_removes_workdir(tmp_path, monkeypatch):
    import dbchat.tools.sandbox as sandbox

    original = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    processes = []

    async def record_spawn(*args, **kwargs):
        proc = await original(*args, **kwargs)
        processes.append((proc, kwargs["cwd"]))
        spawned.set()
        return proc

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", record_spawn)
    task = asyncio.create_task(
        run_python_code("while True: pass", artifacts_dir=tmp_path, timeout_s=30)
    )
    await asyncio.wait_for(spawned.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    from pathlib import Path

    proc, directory = processes[0]
    assert proc.returncode is not None
    assert not await asyncio.to_thread(Path(directory).exists)


@pytest.mark.parametrize("kind", [DialectKind.MYSQL, DialectKind.POSTGRESQL])
def test_connections_request_read_only_sessions(kind, monkeypatch):
    dialect = get_dialect(kind)
    driver = MagicMock()
    monkeypatch.setattr(type(dialect), "import_driver", lambda self: driver)
    dialect.connect(DatabaseProfile(id="audit", database="audit"))
    options = driver.connect.call_args.kwargs
    if kind == DialectKind.MYSQL:
        assert options["init_command"] == "SET SESSION TRANSACTION READ ONLY"
    else:
        assert "default_transaction_read_only=on" in options["options"]
        assert "search_path=public" in options["options"]


async def test_read_only_setup_failure_prevents_query(monkeypatch):
    dialect = get_dialect(DialectKind.DAMENG)
    connection = MagicMock()
    connection.cursor.return_value.execute.side_effect = RuntimeError("read-only setup failed")
    lease = MagicMock(dialect=dialect, connection=connection)
    settings = Settings(_env_file=None)
    executor = SqlExecutor(lease, settings)
    monkeypatch.setattr(executor, "_ensure_statement_timeout", AsyncMock())
    from dbchat.domain.errors import DbError

    with pytest.raises(DbError):
        await executor._execute("SELECT 1 FROM DUAL", 200)
    connection.cursor.return_value.execute.assert_called_once_with("SET TRANSACTION READ ONLY")
    connection.cursor.return_value.close.assert_called_once()
    lease.cursor.assert_not_called()
