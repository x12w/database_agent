"""SSE 层：帧格式、13 种线上事件的往返、心跳、断连取消。

断连这一条是重点：历史实现里用户关掉页面后，那一轮 LLM 调用、工具任务和
数据库连接会继续跑到自然结束。这里断言"断开 → 运行被取消 → 连接租约归零"。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx
import pytest
from pydantic import TypeAdapter
from starlette.requests import Request

from dbchat.api import sse as api_sse
from dbchat.app import create_app
from dbchat.config import Settings
from dbchat.db.connection import ConnectionManager
from dbchat.di import build_context, get_chat_use_case
from dbchat.events.base import ErrorCode, RunStats, Stage, StageView
from dbchat.events.chat import (
    CHAT_EVENT_TYPES,
    ActivityFinished,
    ActivityStarted,
    AnswerDelta,
    AnswerReset,
    ArtifactCreated,
    ChatEvent,
    QuestionAsked,
    RunFailed,
    RunFinished,
    RunStarted,
    StageFinished,
    StageRetried,
    StageStarted,
    ThoughtDelta,
)
from dbchat.events.sse import HEARTBEAT_SECONDS, heartbeat, to_sse
from dbchat.storage import Storage

_EVENT_ADAPTER: TypeAdapter[ChatEvent] = TypeAdapter(ChatEvent)


def sample_events() -> list[ChatEvent]:
    """13 种线上事件各一条（字段取值覆盖可空字段的两侧）。"""
    from dbchat.events.base import ActivityKind, ProfileView

    base = {"ts": 1_700_000_000.0, "run_id": "run-1"}
    return [
        RunStarted(
            seq=1,
            **base,
            profile=ProfileView(id="p1", name="培训考试库", dialect_label="MySQL"),
            stages=[StageView(stage=Stage.UNDERSTANDING, label="理解问题")],
            will_load_schema=True,
        ),
        StageStarted(seq=2, **base, stage=Stage.QUERYING, label="查询数据", index=1, total=5),
        StageFinished(seq=3, **base, stage=Stage.QUERYING, ok=True, summary="完成", elapsed_ms=12),
        StageRetried(
            seq=4, **base, stage=Stage.QUERYING, attempt=2, max_attempts=5, reason="结果为空"
        ),
        ActivityStarted(
            seq=5, **base, activity_id="a1", kind=ActivityKind.QUERY, label="正在查询数据",
            target="students",
        ),
        ActivityFinished(
            seq=6, **base, activity_id="a1", ok=True, summary="返回 3 行", row_count=3,
            elapsed_ms=8, sql_preview=None, error_detail=None, artifact_ids=["c1"],
        ),
        ThoughtDelta(seq=7, **base, text="先看看 students 表"),
        AnswerDelta(seq=8, **base, text="共有 42 名学生"),
        AnswerReset(seq=9, **base),
        ArtifactCreated(
            seq=10, **base, artifact_id="c1", kind="chart", url="/api/artifacts/c1.png",
            title="班级分布",
        ),
        QuestionAsked(
            seq=11, **base, question_id="q1", label="status 列是什么含义", table="students",
            column="status", samples=["01", "02"],
        ),
        RunFinished(
            seq=12, **base, status="ok", elapsed_ms=1200, stats=RunStats(queries=2, rows=42, charts=1),
            answer_markdown="共有 42 名学生。", caveats=[],
        ),
        RunFailed(
            seq=13, **base, code=ErrorCode.DB_AUTH, message="数据库用户名或密码错误，请检查连接配置",
            retryable=False, error_detail=None,
        ),
    ]


def parse_frame(frame: str) -> tuple[str, ChatEvent]:
    """把一个 SSE 帧解回 `(事件名, 事件)`。"""
    lines = frame.rstrip("\n").split("\n")
    assert lines[0].startswith("event: "), frame
    assert lines[1].startswith("data: "), frame
    return lines[0][len("event: ") :], _EVENT_ADAPTER.validate_json(lines[1][len("data: ") :])


def test_frame_format_is_event_and_data():
    for event in sample_events():
        frame = to_sse(event)
        assert frame == f"event: {event.type}\ndata: {event.model_dump_json(exclude_none=False)}\n\n"
        assert frame.endswith("\n\n")


def test_all_thirteen_chat_events_round_trip():
    events = sample_events()
    assert len(events) == len(CHAT_EVENT_TYPES) == 13
    assert [event.type for event in events] == list(CHAT_EVENT_TYPES)

    for event in events:
        name, parsed = parse_frame(to_sse(event))
        assert name == event.type
        assert parsed == event
        assert json.loads(to_sse(event).split("data: ", 1)[1])["type"] == event.type


def test_heartbeat_frame_is_a_comment():
    assert heartbeat() == ": ping\n\n"


async def test_heartbeats_are_emitted_while_idle(monkeypatch):
    """长时间没有事件时，按 HEARTBEAT_SECONDS 发注释帧。"""
    monkeypatch.setattr(api_sse, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(api_sse, "DISCONNECT_POLL_SECONDS", 0.02)

    async def slow_source() -> AsyncIterator[object]:
        await anyio.sleep(0.2)
        yield sample_events()[0]

    frames = [chunk async for chunk in api_sse._frames(
        _RequestStub(),
        slow_source(),
        encode=api_sse._chat_frame,
        on_failure=lambda exc, seq, run_id: "unused",
        run_id="run-1",
        label="测试",
    )]

    pings = [frame for frame in frames if frame == ": ping\n\n"]
    assert len(pings) >= 2, frames
    assert frames[-1].startswith("event: run.started")


class _RequestStub:
    """只用 `is_disconnected` 的替身（心跳测试不涉及断连）。"""

    async def is_disconnected(self) -> bool:
        return False


# --------------------------------------------------------------- 断连取消


class _DummyCursor:
    """假驱动游标：这一组测试只关心租约的借还，不关心查询结果。"""

    description: list[tuple] = [("table_name",)]

    def execute(self, sql, params=None):  # noqa: ANN001, ANN201 - 驱动协议
        return None

    def fetchall(self) -> list[dict]:
        return []

    def fetchone(self) -> None:
        return None

    def close(self) -> None:
        return None


class _DummyConnection:
    def cursor(self):
        return _DummyCursor()

    def close(self) -> None:
        return None


class CountingConnectionManager(ConnectionManager):
    """真实的注册表/锁/租约语义 + 假驱动。

    `acquire()` 走的是 `ConnectionManager` 的实现（锁、复用、归还都是真的），
    只把"建连接"换成假对象 —— 于是 `in_use` 能真实反映"客户端断开后租约有没有
    收回来"。
    """

    def __init__(self) -> None:
        super().__init__(connect_timeout_s=1)
        self.in_use = 0
        self.peak_in_use = 0

    async def _ensure_live(self, entry):  # noqa: ANN001, ANN201 - 基类签名
        if entry.connection is None:
            entry.connection = _DummyConnection()
            entry.created_at = 0.0
        return entry.connection

    @asynccontextmanager
    async def acquire(self, profile):  # type: ignore[override]
        self.in_use += 1
        self.peak_in_use = max(self.peak_in_use, self.in_use)
        try:
            async with super().acquire(profile) as lease:
                yield lease
        finally:
            self.in_use -= 1


class HangingChatUseCase:
    """借住租约、发一条事件后一直等下去的假用例。

    断连时必须被取消：取消打在 `anyio.sleep` 上，`finally` 里归还租约。
    `hang=False` 时正常跑完（对照组）。
    """

    def __init__(self, ctx, *, hang: bool = True) -> None:
        self._ctx = ctx
        self._hang = hang
        self.cancelled = False

    async def run(self, *, message: str, ctx) -> AsyncIterator[ChatEvent]:  # noqa: ANN001
        from dbchat.events.base import ProfileView

        try:
            async with self._ctx.lease(ctx.profile):
                yield RunStarted(
                    seq=1,
                    ts=1.0,
                    run_id=ctx.run_id,
                    profile=ProfileView(id=ctx.profile.id, name="测试库", dialect_label="MySQL"),
                )
                if self._hang:
                    await anyio.sleep(3600)  # 客户端断开后，取消应该打在这里
                yield RunFinished(seq=2, ts=2.0, run_id=ctx.run_id, answer_markdown="42")
        finally:
            self.cancelled = True


@pytest.fixture
def settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(var_dir=tmp_path, debug=False, llm_api_key="")


@pytest.fixture
def manager() -> CountingConnectionManager:
    return CountingConnectionManager()


@pytest.fixture
def ctx(settings: Settings, manager: CountingConnectionManager):
    storage = Storage(settings)
    storage.migrate()
    context = build_context(
        settings, storage=storage, connections=manager, schema_store=None, tools=None, sandbox=None
    )
    yield context
    storage.close()


@pytest.fixture
def use_case(ctx) -> HangingChatUseCase:
    return HangingChatUseCase(ctx)


@pytest.fixture
def app(settings: Settings, ctx, use_case: HangingChatUseCase):
    application = create_app(settings, context=ctx, build_rag=False)
    application.dependency_overrides[get_chat_use_case] = lambda: use_case
    return application


async def test_disconnect_cancels_run_and_releases_lease(
    app, ctx, manager: CountingConnectionManager, use_case: HangingChatUseCase, monkeypatch
):
    """客户端断开 → 运行被取消 → 租约归还、计数器归零。"""
    monkeypatch.setattr(api_sse, "DISCONNECT_POLL_SECONDS", 0.02)
    monkeypatch.setattr(Request, "is_disconnected", _disconnected_after(0))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await asyncio.wait_for(
            client.get("/api/chat/stream", params={"message": "有多少学生？"}), timeout=10
        )

    # 断连前那一条事件正常到达（连接不是"一开始就死"）。
    assert "event: run.started" in response.text
    assert use_case.cancelled, "运行没有被取消：用例的 finally 没跑到"
    assert manager.in_use == 0, "连接租约没有归还"
    assert ctx.in_use_leases == 0
    assert manager.peak_in_use == 1


def _disconnected_after(calls: int):
    """`Request.is_disconnected` 的替身：第 `calls + 1` 次询问起返回 True。"""
    state = {"n": 0}

    async def fake(self) -> bool:  # noqa: ANN001
        state["n"] += 1
        return state["n"] > calls

    return fake


async def test_run_that_finishes_normally_releases_lease(
    settings: Settings, ctx, manager: CountingConnectionManager
):
    """对照组：跑完的运行同样归还租约，且两帧都通过契约发出。"""
    completing = HangingChatUseCase(ctx, hang=False)
    application = create_app(settings, context=ctx, build_rag=False)
    application.dependency_overrides[get_chat_use_case] = lambda: completing

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/api/chat/stream", params={"message": "有多少学生？"})

    names = [
        parse_frame(block + "\n\n")[0]
        for block in response.text.split("\n\n")
        if block.startswith("event: ")
    ]
    assert names == ["run.started", "run.finished"]
    assert manager.in_use == 0
    assert ctx.in_use_leases == 0


def test_heartbeat_interval_is_positive():
    assert HEARTBEAT_SECONDS > 0
