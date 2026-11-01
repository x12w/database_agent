"""API 层：路由可达性、404 语义、SPA 兜底、SSE 往返、管理员闭环。

全程不连真实数据库、不联网：

* 连接用 `FakeConnectionManager`（真实的注册表/锁/租约 + 假驱动游标）；
* RAG 索引用假对象（真的那个会去加载嵌入模型）；
* 用例用假实现经 `dependency_overrides` 注入 —— 这正是 DI 存在的意义。
"""

from __future__ import annotations

import json
import pathlib
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

from dbchat.api.auth import authorize
from dbchat.api.failures import ApiFailure
from dbchat.app import create_app
from dbchat.config import Settings
from dbchat.db.connection import ConnectionManager, ConnectionProbe
from dbchat.di import build_context, get_chat_use_case
from dbchat.domain.profile import DatabaseProfile
from dbchat.events.base import ErrorCode, ProfileView, RunStats
from dbchat.events.chat import (
    AnswerDelta,
    ArtifactCreated,
    ChatEvent,
    RunFailed,
    RunFinished,
    RunStarted,
    StageStarted,
    ThoughtDelta,
)
from dbchat.storage import Storage

_EVENT_ADAPTER: TypeAdapter[ChatEvent] = TypeAdapter(ChatEvent)

#: 本层必须提供的全部路径（方法, 路径）。新增接口时要一起改这里。
REQUIRED_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/health"),
    ("GET", "/api/databases"),
    ("POST", "/api/databases"),
    ("PUT", "/api/databases/{profile_id}"),
    ("DELETE", "/api/databases/{profile_id}"),
    ("GET", "/api/databases/{profile_id}/status"),
    ("POST", "/api/databases/{profile_id}/refresh"),
    ("POST", "/api/databases/{profile_id}/rebuild-cache"),
    ("GET", "/api/tables"),
    ("GET", "/api/session/{session_id}/table-filter"),
    ("POST", "/api/session/{session_id}/table-filter"),
    ("GET", "/api/chat/stream"),
    ("GET", "/api/admin/questions"),
    ("POST", "/api/admin/questions/{question_id}/answer"),
    ("POST", "/api/admin/questions/{question_id}/ignore"),
    ("GET", "/api/admin/explore-database"),
    ("GET", "/api/artifacts/{name}"),
)


# ------------------------------------------------------------------ 假驱动


class _FakeCursor:
    """按 SQL 关键词吐固定结果集的最小游标。"""

    def __init__(self, tables: list[dict], columns: list[dict]) -> None:
        self._tables = tables
        self._columns = columns
        self._rows: list[dict] = []
        self._names: list[str] = []

    def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
        if "information_schema.COLUMNS" in sql:
            self._rows, self._names = self._columns, ["table_name", "column_name"]
        elif "information_schema.TABLES" in sql:
            self._rows = self._tables
            self._names = ["table_name", "table_type", "table_rows", "table_comment"]
        else:  # pragma: no cover - 本层只会走这两条内省 SQL
            self._rows, self._names = [], []

    @property
    def description(self) -> list[tuple]:
        return [(name,) for name in self._names]

    def fetchall(self) -> list[dict]:
        return list(self._rows)

    def fetchone(self) -> dict | None:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, tables: list[dict], columns: list[dict]) -> None:
        self._tables = tables
        self._columns = columns

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._tables, self._columns)

    def close(self) -> None:
        return None


class FakeConnectionManager(ConnectionManager):
    """真实的 `ConnectionManager`（锁、复用、注册表），只把驱动换成假的。"""

    TABLES = [
        {"table_name": "students", "table_type": "BASE TABLE", "table_rows": 42, "table_comment": "学生表"},
        {"table_name": "scores", "table_type": "BASE TABLE", "table_rows": 7, "table_comment": ""},
    ]
    COLUMNS = [
        {"table_name": "students", "column_name": "id"},
        {"table_name": "students", "column_name": "name"},
        {"table_name": "scores", "column_name": "student_id"},
    ]

    def __init__(self) -> None:
        super().__init__(connect_timeout_s=1)
        self.in_use = 0
        self.probes: list[str] = []
        self.closed: list[str] = []

    async def _ensure_live(self, entry):  # noqa: ANN001, ANN201 - 基类签名
        if entry.connection is None:
            entry.connection = _FakeConnection(self.TABLES, self.COLUMNS)
            entry.created_at = time.time()
        return entry.connection

    async def probe(self, profile: DatabaseProfile) -> ConnectionProbe:
        self.probes.append(profile.id)
        return ConnectionProbe(
            success=True,
            message=f"连接成功（{profile.kind.value}），{profile.database} 包含 2 张表",
            table_count=2,
        )

    async def close(self, profile_id: str) -> None:
        self.closed.append(profile_id)
        await super().close(profile_id)

    @asynccontextmanager
    async def acquire(self, profile: DatabaseProfile):  # type: ignore[override]
        self.in_use += 1
        try:
            async with super().acquire(profile) as lease:
                yield lease
        finally:
            self.in_use -= 1


class FakeRAG:
    """RAG 索引替身：真的那个要加载嵌入模型、还要联网下模型。"""

    def __init__(self) -> None:
        self.ready = False
        self.state = type("State", (), {"size": 0, "fallback": True})()
        self.admin_docs: list[tuple[str, str | None]] = []

    async def abuild(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003
        return False

    async def abuild_admin_index(self, admin_doc=None, profile_id=None, *, force=True) -> bool:  # noqa: ANN001
        self.admin_docs.append((admin_doc or "", profile_id))
        return True


class ScriptedChatUseCase:
    """按剧本产事件的假聊天用例。"""

    def __init__(self, script: list[ChatEvent]) -> None:
        self._script = script
        self.seen: list[tuple[str, str, int]] = []

    async def run(self, *, message: str, ctx) -> AsyncIterator[ChatEvent]:  # noqa: ANN001
        self.seen.append((message, ctx.conversation_id, len(ctx.history)))
        for event in self._script:
            yield event


class FailingChatUseCase:
    """抛 `DbError` 的假用例：验证异常变成中文失败事件、而不是原始报文。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, *, message: str, ctx) -> AsyncIterator[ChatEvent]:  # noqa: ANN001
        raise self._exc
        yield  # pragma: no cover - 让函数成为异步生成器


class FakeExploreUseCase:
    """按剧本产探索事件的假用例。"""

    def __init__(self, script: list[object]) -> None:
        self._script = script
        self.seen: list[tuple[str, object]] = []

    async def run(self, *, profile, scope) -> AsyncIterator[object]:  # noqa: ANN001
        self.seen.append((profile.id, scope))
        for event in self._script:
            yield event


# ------------------------------------------------------------------ fixture


@pytest.fixture
def settings(tmp_path: pathlib.Path) -> Settings:
    """隔离的运行时目录 + 一份临时前端产物（不依赖仓库里的 web/dist）。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>dbchat</title>", encoding="utf-8")
    (dist / "assets" / "app.css").write_text("body{}", encoding="utf-8")
    return Settings(var_dir=tmp_path, web_dist_dir=dist, debug=False, llm_api_key="")


@pytest.fixture
def storage(settings: Settings) -> Iterator[Storage]:
    instance = Storage(settings)
    instance.migrate()
    yield instance
    instance.close()


@pytest.fixture
def manager() -> FakeConnectionManager:
    return FakeConnectionManager()


@pytest.fixture
def ctx(settings: Settings, storage: Storage, manager: FakeConnectionManager):
    return build_context(
        settings,
        storage=storage,
        connections=manager,
        rag=FakeRAG(),
        schema_store=None,
        tools=None,
        sandbox=None,
    )


@pytest.fixture
def app(settings: Settings, ctx):
    application = create_app(settings, context=ctx, build_rag=False)
    application.dependency_overrides[authorize] = lambda: "admin"
    return application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as instance:
        yield instance


def scripted_events(run_id: str) -> list[ChatEvent]:
    base = {"ts": time.time(), "run_id": run_id}
    return [
        RunStarted(
            seq=1, **base,
            profile=ProfileView(id="p1", name="测试库", dialect_label="MySQL"),
        ),
        StageStarted(seq=2, **base, stage="understanding", label="理解问题", index=1, total=5),
        ThoughtDelta(seq=3, **base, text="先看 students 表"),
        AnswerDelta(seq=4, **base, text="共 42 人"),
        ArtifactCreated(seq=5, **base, artifact_id="a1", kind="chart", url="/api/artifacts/a1.png"),
        RunFinished(seq=6, **base, status="ok", stats=RunStats(queries=1, rows=42, charts=1),
                    answer_markdown="共 42 人"),
    ]


def frames_of(body: str) -> list[tuple[str, ChatEvent]]:
    """把响应体解析回 `(事件名, 事件)` —— 与前端 `parseSseBlock` 同一套切分。"""
    result: list[tuple[str, ChatEvent]] = []
    for block in body.split("\n\n"):
        if not block.startswith("event: "):
            continue
        lines = block.split("\n")
        name = lines[0][len("event: ") :]
        data = lines[1][len("data: ") :]
        result.append((name, _EVENT_ADAPTER.validate_json(data)))
    return result


def iter_api_routes(routes) -> Iterator[APIRoute]:  # noqa: ANN001
    """递归展开路由表（FastAPI 0.141 里 `include_router` 会包一层）。"""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        inner = getattr(route, "original_router", None)
        nested = getattr(inner, "routes", None) if inner is not None else getattr(route, "routes", None)
        if not nested:
            continue
        try:
            yield from iter_api_routes(nested)
        except AttributeError:  # pragma: no cover - 静态文件挂载
            continue


# ------------------------------------------------------------------ 基础


async def test_health(client: httpx.AsyncClient):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is False  # 还没借过连接
    assert body["llm_available"] is False
    assert body["rag_ready"] is False


async def test_databases_crud_status_refresh_rebuild(
    client: httpx.AsyncClient, manager: FakeConnectionManager, ctx
):
    listed = await client.get("/api/databases")
    assert listed.status_code == 200
    default_profile = listed.json()["profiles"][0]
    assert listed.json()["current"] == default_profile["id"]

    created = await client.post(
        "/api/databases",
        json={
            "name": "电商库",
            "host": "127.0.0.1",
            "port": 3308,
            "user": "root",
            "password": "secret",
            "database": "ecommerce",
            "db_type": "mysql",
            "description": "测试用",
        },
    )
    assert created.status_code == 201, created.text
    new_id = created.json()["profile"]["id"]
    assert "password" not in created.json()["profile"]
    assert manager.probes == [new_id]  # 新增前先自检连接

    updated = await client.put(f"/api/databases/{new_id}", json={"name": "电商库（改）"})
    assert updated.status_code == 200
    assert updated.json()["profile"]["name"] == "电商库（改）"
    assert new_id in manager.closed  # 参数变了就把连接丢掉

    status = await client.get(f"/api/databases/{new_id}/status")
    assert status.status_code == 200
    assert status.json()["table_count"] == 2
    assert status.json()["tables"] == ["students", "scores"]
    assert status.json()["suggestions"]

    refreshed = await client.post(f"/api/databases/{new_id}/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["table_count"] == 2

    await ctx.explorations.asave(new_id, "报告", [])
    rebuilt = await client.post(f"/api/databases/{new_id}/rebuild-cache")
    assert rebuilt.status_code == 200
    assert "探索记录已清除" in rebuilt.json()["message"]
    assert await ctx.explorations.ais_explored(new_id) is False

    deleted = await client.delete(f"/api/databases/{new_id}")
    assert deleted.status_code == 200
    assert await ctx.profiles.aget(new_id) is None

    missing = await client.delete(f"/api/databases/{new_id}")
    assert missing.status_code == 404


async def test_bad_profile_returns_404_everywhere(client: httpx.AsyncClient):
    """未知 profile 一律 404，且发生在流开始之前。"""
    for method, url in (
        ("GET", "/api/databases/nope/status"),
        ("POST", "/api/databases/nope/refresh"),
        ("POST", "/api/databases/nope/rebuild-cache"),
        ("GET", "/api/tables?db_id=nope"),
        ("GET", "/api/chat/stream?message=hi&db_id=nope"),
        ("GET", "/api/admin/explore-database?db_id=nope"),
    ):
        response = await client.request(method, url)
        assert response.status_code == 404, (method, url, response.text)
        assert "nope" in response.json()["detail"]


async def test_unsupported_dialect_is_rejected(client: httpx.AsyncClient):
    response = await client.post(
        "/api/databases", json={"name": "x", "database": "x", "db_type": "oracle"}
    )
    assert response.status_code == 400
    assert "不支持的数据库类型" in response.json()["detail"]


async def test_tables_lists_every_table(client: httpx.AsyncClient):
    response = await client.get("/api/tables")
    assert response.status_code == 200
    tables = response.json()["tables"]
    assert [table["name"] for table in tables] == ["students", "scores"]
    assert tables[0]["row_count"] == 42
    assert tables[0]["column_count"] == 2
    assert tables[0]["comment"] == "学生表"


async def test_table_filter_round_trip(client: httpx.AsyncClient):
    empty = await client.get("/api/session/sess-1/table-filter")
    assert empty.status_code == 404

    saved = await client.post("/api/session/sess-1/table-filter", json={"tables": ["students"]})
    assert saved.status_code == 200
    assert saved.json()["table_filter"] == ["students"]

    # 一次读取不会把筛选清掉（GET 是只读的）。
    again = await client.get("/api/session/sess-1/table-filter")
    assert again.json()["table_filter"] == ["students"]

    cleared = await client.post("/api/session/sess-1/table-filter", json={"tables": None})
    assert cleared.json()["table_filter"] is None
    assert (await client.get("/api/session/sess-1/table-filter")).json()["table_filter"] is None


async def test_artifacts_are_served_from_artifacts_dir(client: httpx.AsyncClient, settings: Settings):
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (settings.artifacts_dir / "chart.png").write_bytes(b"\x89PNG\r\n")

    response = await client.get("/api/artifacts/chart.png")
    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n"

    assert (await client.get("/api/artifacts/nope.png")).status_code == 404
    assert (await client.get("/api/artifacts/..%2F..%2Fetc%2Fpasswd")).status_code == 404


# ------------------------------------------------------------------ 前端兜底


async def test_spa_fallback_serves_index_and_assets(client: httpx.AsyncClient):
    for path in ("/", "/admin", "/dev"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")

    asset = await client.get("/assets/app.css")
    assert asset.status_code == 200
    assert asset.text == "body{}"


async def test_api_paths_never_fall_through_to_the_spa(client: httpx.AsyncClient):
    for path in ("/api", "/api/nope", "/api/admin/nope"):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith("application/json")
        assert "<!doctype html>" not in response.text.lower()


async def test_missing_frontend_build_is_explained(settings: Settings, ctx):
    """前端没构建时给一句能照做的话，而不是 404 的 JSON。"""
    settings.web_dist_dir = settings.var_dir / "nope"
    application = create_app(settings, context=ctx, build_rag=False)
    application.dependency_overrides[authorize] = lambda: "admin"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 404
    assert "前端尚未构建" in response.text


# ------------------------------------------------------------------ 聊天 SSE


async def test_chat_stream_round_trips_scripted_events(app, ctx, manager: FakeConnectionManager):
    use_case = ScriptedChatUseCase(scripted_events("run-1"))
    app.dependency_overrides[get_chat_use_case] = lambda: use_case

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/chat/stream",
            params={"message": "有多少学生？", "session_id": "sess-9", "db_id": "default"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"

    parsed = frames_of(response.text)
    assert [name for name, _ in parsed] == [event.type for event in scripted_events("run-1")]
    # 正文能解回同一批事件类型（to_sse 的往返）。
    assert isinstance(parsed[0][1], RunStarted)
    assert isinstance(parsed[-1][1], RunFinished)
    assert parsed[-1][1].answer_markdown == "共 42 人"

    # 会话与运行上下文：假用例收到了消息、会话 id 与历史。
    assert use_case.seen == [("有多少学生？", "sess-9", 0)]
    assert manager.in_use == 0

    # 本轮问答已落库（会话 id 就是前端的 session_id）。
    history = await ctx.conversations.ahistory("sess-9")
    assert history == [
        {"role": "user", "content": "有多少学生？"},
        {"role": "assistant", "content": "共 42 人"},
    ]


async def test_switching_database_rebinds_the_conversation(app, ctx, client: httpx.AsyncClient):
    """同一会话 id 换库时：改归属、丢旧库历史 —— 绝不把 A 库的问答当 B 库上下文。"""
    from dbchat.di import ensure_conversation

    default_profile = await ctx.resolve_profile(None)
    await ensure_conversation(
        ctx, session_id="sess-switch", profile_id=default_profile.id, table_filter=None
    )
    await ctx.conversations.astore_message("sess-switch", "user", "A 库的问题")

    await client.post(
        "/api/databases", json={"name": "另一个库", "database": "other", "db_type": "mysql"}
    )
    other = [p for p in await ctx.profiles.alist_all() if p.database == "other"][0]

    class Echo:
        async def run(self, *, message: str, ctx) -> AsyncIterator[ChatEvent]:  # noqa: ANN001
            yield RunFinished(seq=1, ts=1.0, run_id=ctx.run_id, answer_markdown="B 库回答")

    app.dependency_overrides[get_chat_use_case] = Echo
    await client.get(
        "/api/chat/stream",
        params={"message": "B 库的问题", "session_id": "sess-switch", "db_id": other.id},
    )

    rebound = await ctx.conversations.aget("sess-switch")
    assert rebound is not None
    assert rebound.id == "sess-switch"  # 会话 id 就是前端的 session_id
    assert rebound.profile_id == other.id
    # 旧库的历史已丢，只剩这一轮：A 库的提问不再进 B 库的上下文。
    assert await ctx.conversations.ahistory("sess-switch") == [
        {"role": "user", "content": "B 库的问题"},
        {"role": "assistant", "content": "B 库回答"},
    ]


async def test_overlong_message_is_rejected_before_anything_else():
    """超长提问在依赖层就被拒 —— 不必真的发一个 8KB 的 URL 出去。"""
    from dbchat.api.routes.chat import MAX_MESSAGE_CHARS, validated_message

    class _Run:
        run_id = "run-x"

    with pytest.raises(ApiFailure) as caught:
        await validated_message(_Run(), message="问" * (MAX_MESSAGE_CHARS + 1))

    assert caught.value.code is ErrorCode.VALIDATION
    assert "问题过长" in caught.value.message
    assert await validated_message(_Run(), message="正常提问") == "正常提问"


async def test_chat_stream_records_failed_turn(app, ctx):
    """失败的运行也留下一问一答 —— 否则下一轮模型会复述上一轮的答案。"""
    from dbchat.domain.errors import DbError, DbErrorKind

    app.dependency_overrides[get_chat_use_case] = lambda: FailingChatUseCase(
        DbError(DbErrorKind.NETWORK)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(
            "/api/chat/stream", params={"message": "有多少学生？", "session_id": "sess-fail"}
        )

    history = await ctx.conversations.ahistory("sess-fail")
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert "未能给出回答" in history[1]["content"]


async def test_chat_stream_rejects_empty_message(app, ctx):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/chat/stream", params={"message": "   "})

    assert response.status_code == 200
    ((name, event),) = frames_of(response.text)
    assert name == "run.failed"
    assert isinstance(event, RunFailed)
    assert event.code is ErrorCode.VALIDATION
    assert "请输入问题内容" in event.message


async def test_chat_stream_turns_exceptions_into_chinese_failure(app, ctx):
    from dbchat.domain.errors import DbError, DbErrorKind

    failure = DbError(
        DbErrorKind.AUTH,
        detail="(1045, \"Access denied for user 'root'@'10.0.0.1'\")",
    )
    app.dependency_overrides[get_chat_use_case] = lambda: FailingChatUseCase(failure)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/chat/stream", params={"message": "hi"})

    ((name, event),) = frames_of(response.text)
    assert name == "run.failed"
    assert event.code is ErrorCode.DB_AUTH
    assert event.message == "数据库用户名或密码错误，请检查连接配置"
    # 原始驱动报文只进日志（非调试模式下不出现在事件里）。
    assert event.error_detail is None
    assert "Access denied" not in response.text


async def test_chat_stream_persists_table_filter_into_the_run(app, ctx):
    """会话里存的表筛选（前端第二次提问不再带参数）要进 `RunContext.scope`。"""
    seen: list[object] = []

    class ScopeSpy:
        async def run(self, *, message: str, ctx) -> AsyncIterator[ChatEvent]:  # noqa: ANN001
            seen.append(ctx.scope)
            yield RunFinished(seq=1, ts=1.0, run_id=ctx.run_id)

    app.dependency_overrides[get_chat_use_case] = ScopeSpy
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        saved = await client.post(
            "/api/session/sess-7/table-filter", json={"tables": ["scores"]}
        )
        assert saved.status_code == 200

        # 第一次带参数（参数优先）
        await client.get(
            "/api/chat/stream",
            params={"message": "hi", "session_id": "sess-7", "table_filter": "students"},
        )
        # 第二次不带参数：沿用会话里存的（而不是被清成"不限"）
        await client.get("/api/chat/stream", params={"message": "hi", "session_id": "sess-7"})

    assert seen[0].allows("students") and not seen[0].allows("scores")
    assert seen[1].allows("students") and not seen[1].allows("scores")
    assert (await ctx.conversations.aget("sess-7")).table_filter == ("students",)


# ------------------------------------------------------------------ 管理员


async def test_admin_routes_exist_exactly_once(app):
    inventory: list[tuple[str, str]] = []
    for route in iter_api_routes(app.routes):
        for method in route.methods or ():
            inventory.append((method, route.path))

    for method, path in REQUIRED_ROUTES:
        assert inventory.count((method, path)) == 1, f"{method} {path} 出现 {inventory.count((method, path))} 次"

    assert len(inventory) == len(set(inventory)), f"路由重复: {inventory}"
    admin_paths = {path for _, path in inventory if path.startswith("/api/admin/")}
    assert admin_paths == {
        "/api/admin/questions",
        "/api/admin/questions/{question_id}/answer",
        "/api/admin/questions/{question_id}/ignore",
        "/api/admin/explore-database",
        "/api/admin/settings",
        "/api/admin/settings/test-model",
    }


async def test_admin_question_flow(app, ctx, client: httpx.AsyncClient):
    profile = await ctx.resolve_profile(None)
    question_id = await ctx.clarifications.aadd_question(
        profile.id, "students", "status 列是什么含义", column_name="status", sample_values=["01"]
    )

    pending = await client.get("/api/admin/questions", params={"status": "pending"})
    assert pending.status_code == 200
    assert pending.json()["pending_count"] == 1
    assert pending.json()["questions"][0]["question_id"] == question_id

    # 不带 profile_id 也能定位（前端在未选库时会传 null）——绝不猜一个库去答。
    answered = await client.post(
        f"/api/admin/questions/{question_id}/answer", json={"answer": "01=在读, 02=毕业"}
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["profile_id"] == profile.id
    assert ctx.rag.admin_docs and ctx.rag.admin_docs[-1][1] == profile.id

    answered_list = await client.get("/api/admin/questions", params={"status": "answered"})
    assert answered_list.json()["total_count"] == 1
    assert answered_list.json()["questions"][0]["answer"] == "01=在读, 02=毕业"

    empty = await client.post(f"/api/admin/questions/{question_id}/answer", json={"answer": " "})
    assert empty.status_code == 400
    missing = await client.post("/api/admin/questions/nope/answer", json={"answer": "x"})
    assert missing.status_code == 404


async def test_admin_ignore_question(app, ctx, client: httpx.AsyncClient):
    profile = await ctx.resolve_profile(None)
    question_id = await ctx.clarifications.aadd_question(profile.id, "scores", "这一列是什么")

    ignored = await client.post(
        f"/api/admin/questions/{question_id}/ignore", json={"profile_id": profile.id}
    )
    assert ignored.status_code == 200
    assert (await ctx.clarifications.aget(profile.id, question_id)).status == "ignored"

    filtered = await client.get("/api/admin/questions", params={"status": "ignored"})
    assert filtered.json()["total_count"] == 1
    assert (await client.post("/api/admin/questions/nope/ignore")).status_code == 404


async def test_admin_explore_stream(app, ctx, manager: FakeConnectionManager):
    from dbchat.di import get_explore_use_case
    from dbchat.events.explore import (
        ExploreFinished,
        ExploreProgress,
        ExploreQuestion,
        ExploreStarted,
    )

    profile: DatabaseProfile = await ctx.resolve_profile(None)
    use_case = FakeExploreUseCase(
        [
            ExploreStarted(seq=1, ts=1.0, run_id="e1", profile=ProfileView(id=profile.id, name="库", dialect_label="MySQL")),
            ExploreProgress(seq=2, ts=1.1, run_id="e1", done=1, total=3, current_table="students"),
            ExploreQuestion(seq=3, ts=1.2, run_id="e1", question_id="q1", table="students",
                            column="status", question="这一列是什么", samples=["01"]),
            ExploreFinished(seq=4, ts=2.0, run_id="e1", questions_asked=1, pending_count=1),
        ]
    )
    app.dependency_overrides[get_explore_use_case] = lambda: use_case

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/explore-database", params={"db_id": profile.id})

    assert response.status_code == 200
    names = [
        block.split("\n", 1)[0][len("event: ") :]
        for block in response.text.split("\n\n")
        if block.startswith("event: ")
    ]
    # 探索通道的事件名就是契约里的点号名（不再有 explore_error 那种改名）。
    assert names == ["explore.started", "explore.progress", "explore.question", "explore.finished"]
    assert use_case.seen[0][0] == profile.id


async def test_admin_explore_failure_uses_contract_event(app, ctx):
    from dbchat.di import get_explore_use_case
    from dbchat.events.explore import ExploreFailed

    class Broken:
        async def run(self, *, profile, scope) -> AsyncIterator[object]:  # noqa: ANN001
            raise RuntimeError("驱动挂了")
            yield  # pragma: no cover

    app.dependency_overrides[get_explore_use_case] = Broken
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/admin/explore-database")

    block = response.text.split("\n\n")[0]
    assert block.startswith("event: explore.failed")
    event = ExploreFailed.model_validate(json.loads(block.split("data: ", 1)[1]))
    assert event.message == "服务内部错误，请稍后重试"
    assert "驱动挂了" not in response.text


async def test_use_case_not_implemented_yields_503(app, monkeypatch):
    """用例模块缺失时给 503 + 一句能照着修的提示，而不是 ImportError。"""
    import dbchat.di as di

    def explode(module_name: str, candidates, *, what: str):  # noqa: ANN001
        raise di.HTTPException(status_code=503, detail=f"{what}尚未实现")

    monkeypatch.setattr(di, "_load_factory", explode)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/chat/stream", params={"message": "hi"})

    assert response.status_code == 503
    assert "尚未实现" in response.json()["detail"]
