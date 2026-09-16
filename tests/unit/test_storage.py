"""SQLite 持久化层：仓储 CRUD、管理员问答语义、迁移断言、长会话。"""

from __future__ import annotations

import json

import pytest

from dbchat.config import Settings
from dbchat.domain.profile import DatabaseProfile, DialectKind
from dbchat.storage import (
    Clarification,
    ClarificationRepository,
    ConversationRepository,
    ExplorationRepository,
    MigrationError,
    ProfileRepository,
    Storage,
    extract_mappings,
    import_legacy_json,
)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """隔离的运行时目录（库文件、RAG 索引都落在 tmp_path 下）。"""
    return Settings(var_dir=tmp_path)


@pytest.fixture
def storage(settings) -> Storage:
    db = Storage(settings)
    db.migrate()
    yield db
    db.close()


@pytest.fixture
def conversations(storage) -> ConversationRepository:
    return ConversationRepository(storage)


@pytest.fixture
def clarifications(storage) -> ClarificationRepository:
    return ClarificationRepository(storage)


@pytest.fixture
def explorations(storage) -> ExplorationRepository:
    return ExplorationRepository(storage)


@pytest.fixture
def profiles(storage) -> ProfileRepository:
    return ProfileRepository(storage)


def sample_profile(profile_id: str = "p1", **overrides) -> DatabaseProfile:
    data = {
        "id": profile_id,
        "name": "training_exam_db",
        "host": "127.0.0.1",
        "port": 3307,
        "user": "root",
        "password": "secret",
        "database": "training_exam_db",
        "kind": DialectKind.MYSQL,
        "description": "测试库",
    }
    data.update(overrides)
    return DatabaseProfile(**data)


# ---- 连接与迁移 ----


def test_storage_creates_file_and_applies_migration(settings, tmp_path):
    db = Storage(settings)
    db.migrate()
    assert db.path == tmp_path / "dbchat.sqlite3"
    assert db.path.exists()

    tables = {
        row["name"]
        for row in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "conversations",
        "messages",
        "profiles",
        "clarifications",
        "explorations",
        "schema_migrations",
    } <= tables

    assert db.query_one("PRAGMA journal_mode")[0].lower() == "wal"
    db.close()


def test_migration_is_idempotent(settings):
    db = Storage(settings)
    db.migrate()
    db.migrate()
    versions = [row["version"] for row in db.query("SELECT version FROM schema_migrations")]
    assert versions == ["0001_init"]
    db.close()


def test_reopening_existing_database_keeps_data(settings, conversations):
    convo = conversations.create("p1")
    conversations.store_message(convo.id, "user", "你好")

    reopened = Storage(settings)
    assert ConversationRepository(reopened).message_count(convo.id) == 1
    reopened.close()


# ---- profiles ----


def test_profile_crud(profiles):
    assert profiles.count() == 0
    assert profiles.list_all() == []

    profiles.upsert(sample_profile())
    loaded = profiles.get("p1")
    assert loaded is not None
    assert loaded.display_name == "training_exam_db"
    assert loaded.kind is DialectKind.MYSQL
    assert loaded.port == 3307

    profiles.upsert(sample_profile(name="改名了"))
    assert profiles.get("p1").name == "改名了"
    assert profiles.count() == 1

    assert profiles.delete("p1") is True
    assert profiles.delete("p1") is False
    assert profiles.get("p1") is None


def test_profile_replace_all_drops_missing(profiles):
    profiles.upsert(sample_profile("p1"))
    profiles.upsert(sample_profile("p2"))

    profiles.replace_all([sample_profile("p2", name="保留"), sample_profile("p3")])

    assert sorted(p.id for p in profiles.list_all()) == ["p2", "p3"]
    assert profiles.get("p2").name == "保留"


def test_profile_ensure_default_seeds_only_when_empty(profiles):
    default = sample_profile("default")
    assert [p.id for p in profiles.ensure_default(default)] == ["default"]

    profiles.upsert(sample_profile("other"))
    assert "default" in [p.id for p in profiles.ensure_default(default)]


# ---- conversations ----


def test_conversation_create_get_list(conversations):
    convo = conversations.create("p1", title="第一个会话")
    assert convo.id and len(convo.id) == 12

    loaded = conversations.get(convo.id)
    assert loaded is not None
    assert loaded.title == "第一个会话"
    assert loaded.profile_id == "p1"
    assert loaded.message_count == 0
    assert loaded.table_filter is None

    conversations.create("p2")
    assert len(conversations.list_all()) == 2
    assert [c.profile_id for c in conversations.list_all("p1")] == ["p1"]
    assert conversations.count() == 2
    assert conversations.count("p2") == 1
    assert conversations.get("nope") is None


def test_conversation_table_filter_round_trip(conversations):
    convo = conversations.create("p1", table_filter=["students", "scores"])
    assert conversations.get(convo.id).table_filter == ("students", "scores")

    assert conversations.set_table_filter(convo.id, None) is True
    assert conversations.get(convo.id).table_filter is None
    assert conversations.set_table_filter("nope", ["x"]) is False


def test_conversation_auto_title_from_first_user_message(conversations):
    convo = conversations.create("p1")
    conversations.store_message(convo.id, "assistant", "你好，请问有什么可以帮你？")
    assert conversations.get(convo.id).title == ""

    conversations.store_message(convo.id, "user", "统计每个班级的平均分")
    assert conversations.get(convo.id).title == "统计每个班级的平均分"


def test_conversation_rename_and_delete_cascades(conversations, storage):
    convo = conversations.create("p1")
    conversations.store_message(convo.id, "user", "hi")
    assert conversations.rename(convo.id, "新标题") is True
    assert conversations.rename("nope", "x") is False
    assert conversations.get(convo.id).title == "新标题"

    assert conversations.delete(convo.id) is True
    assert conversations.delete(convo.id) is False
    assert storage.query("SELECT * FROM messages") == []


def test_conversation_with_more_than_20_messages_survives_and_reopens(conversations, storage):
    """历史实现只留最近 20 条；现在必须一条不少地存下来并能重开。"""
    convo = conversations.create("p1")
    for i in range(25):
        role = "user" if i % 2 == 0 else "assistant"
        conversations.store_message(convo.id, role, f"第 {i} 条")

    reopened = ConversationRepository(storage).detail(convo.id)
    assert reopened is not None
    assert reopened.conversation.message_count == 25
    assert len(reopened.messages) == 25
    assert [m.seq for m in reopened.messages] == list(range(1, 26))
    assert reopened.messages[0].content == "第 0 条"
    assert reopened.messages[-1].content == "第 24 条"

    history = conversations.history(convo.id)
    assert len(history) == 25
    assert history[0] == {"role": "user", "content": "第 0 条"}


def test_conversation_history_limit_takes_the_most_recent(conversations):
    convo = conversations.create("p1")
    for i in range(30):
        conversations.store_message(convo.id, "user", f"m{i}")

    recent = conversations.history(convo.id, limit=4)
    assert [item["content"] for item in recent] == ["m26", "m27", "m28", "m29"]


def test_storing_message_in_missing_conversation_fails_loudly(conversations):
    """写进不存在的会话必须报错，而不是静默产生孤儿消息。"""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conversations.store_message("missing", "user", "hi")


def test_message_ordering_is_append_order(conversations):
    convo = conversations.create("p1")
    for i in range(5):
        conversations.store_message(convo.id, "user", f"m{i}")
    conversations.store_message(convo.id, "assistant", "回答")

    assert [m.content for m in conversations.messages(convo.id)] == [
        "m0",
        "m1",
        "m2",
        "m3",
        "m4",
        "回答",
    ]


# ---- clarifications ----


def test_extract_mappings_recognizes_documented_patterns():
    assert extract_mappings("01=在职, 02=离职") == {"01": "在职", "02": "离职"}
    assert extract_mappings("A代表优秀, B代表良好") == {"A": "优秀", "B": "良好"}
    assert extract_mappings("01:在职, 02:离职") == {"01": "在职", "02": "离职"}
    assert extract_mappings("TECH:技术类") == {"TECH": "技术类"}
    assert extract_mappings("TECH:技术类, MGT:管理类") == {
        "TECH": "技术类",
        "MGT": "管理类",
    }
    assert extract_mappings("'01'=在职") == {"01": "在职"}
    # 历史已知怪癖，原样保留：「状态码」被当成了键、值里带着「0表示正常」。
    # 这里的断言是为了钉住行为（而不是钉住"正确"）—— 与旧实现逐字符一致。
    assert extract_mappings("状态码：0表示正常；1表示禁用") == {
        "状态码": "0表示正常",
        "1": "禁用",
    }


def test_extract_mappings_returns_empty_without_mappings():
    assert extract_mappings("该列的取值请参考 HR 系统") == {}
    assert extract_mappings("") == {}
    # 键过长的一律不算映射（避免把整句话误判成编码表）
    assert extract_mappings("这是一个非常长的键" + "x" * 30 + "=值") == {}


def test_clarification_question_answer_round_trip(clarifications):
    question_id = clarifications.add_question(
        "p1",
        "students",
        "请问 `gender` 列的 01/02 表示什么？",
        column_name="gender",
        sample_values=["01", "02"],
        agent_guess="01=男，02=女",
    )
    assert len(question_id) == 12

    pending = clarifications.get("p1", question_id)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.sample_values == ["01", "02"]
    assert pending.agent_guess == "01=男，02=女"
    assert pending.answer == ""
    assert pending.created_at

    assert clarifications.answer_question("p1", question_id, "01=男, 02=女") is True
    answered = clarifications.get("p1", question_id)
    assert answered.status == "answered"
    assert answered.answer == "01=男, 02=女"
    assert answered.answered_at

    # 未登记的 id
    assert clarifications.answer_question("p1", "nope", "x") is False
    assert clarifications.ignore_question("p1", "nope") is False


def test_clarification_ignore(clarifications):
    question_id = clarifications.add_question("p1", "t", "问题?")
    assert clarifications.ignore_question("p1", question_id) is True
    assert clarifications.get("p1", question_id).status == "ignored"
    assert clarifications.get_pending("p1") == []
    assert clarifications.get_all("p1")[0].question_id == question_id


def test_clarification_answer_can_be_corrected(clarifications):
    """允许管理员修正已答内容（历史语义）。"""
    question_id = clarifications.add_question("p1", "t", "问题?")
    clarifications.answer_question("p1", question_id, "01=男")
    clarifications.answer_question("p1", question_id, "01=男, 02=女")
    assert clarifications.get("p1", question_id).answer == "01=男, 02=女"


def test_clarification_lists_and_counts(clarifications):
    a = clarifications.add_question("p1", "students", "q1", column_name="gender")
    b = clarifications.add_question("p1", "students", "q2", column_name="age")
    c = clarifications.add_question("p1", "scores", "q3")
    clarifications.answer_question("p1", b, "18")

    assert clarifications.get_pending_count("p1") == 2
    assert {e.question_id for e in clarifications.get_pending("p1")} == {a, c}
    assert [e.question_id for e in clarifications.get_all_answered("p1")] == [b]
    assert len(clarifications.get_all("p1")) == 3
    assert clarifications.count("p1") == 3
    assert clarifications.get_all_profile_ids() == ["p1"]


def test_clarification_exact_lookup_is_case_insensitive(clarifications):
    qid = clarifications.add_question("p1", "Students", "q", column_name="Gender")
    clarifications.answer_question("p1", qid, "01=男")

    assert [e.question_id for e in clarifications.get_clarification("p1", "students", "gender")] == [qid]
    # 表级查询（column 留空）返回该表所有列 —— 历史语义
    assert [e.question_id for e in clarifications.get_clarification("p1", "STUDENTS")] == [qid]
    assert clarifications.get_clarification("p1", "scores") == []
    # 未回答的问题不出现在精确查询里
    clarifications.add_question("p1", "scores", "q2", column_name="x")
    assert clarifications.get_clarification("p1", "scores", "x") == []


def test_clarification_pending_summary_and_known_columns(clarifications):
    clarifications.add_question("p1", "students", "q1", column_name="gender")
    clarifications.add_question("p1", "students", "q2")
    answered = clarifications.add_question("p1", "scores", "q3", column_name="Grade")
    clarifications.answer_question("p1", answered, "A=优秀")
    ignored = clarifications.add_question("p1", "scores", "q4", column_name="gone")
    clarifications.ignore_question("p1", ignored)

    assert clarifications.get_pending_summary("p1") == {"students": ["`gender`", "（整表）"]}
    assert clarifications.get_known_columns("p1") == {
        ("students", "gender"),
        ("students", ""),
        ("scores", "grade"),
    }


def test_clarification_exploration_progress(clarifications):
    q1 = clarifications.add_question("p1", "students", "q1")
    clarifications.answer_question("p1", q1, "a")
    clarifications.add_question("p1", "scores", "q2")

    progress = clarifications.get_exploration_progress("p1", {"students": 10, "scores": 5})
    assert progress == {
        "total_columns": 15,
        "clarified_columns": 1,
        "pending_columns": 1,
        "unknown_columns": 13,
        "tables_with_pending": ["scores"],
    }


def test_clarification_rag_document(clarifications):
    assert clarifications.build_rag_document("p1") == ""

    q1 = clarifications.add_question(
        "p1", "students", "q1", column_name="gender", sample_values=["01", "02"]
    )
    q2 = clarifications.add_question("p1", "students", "q2")
    clarifications.answer_question("p1", q1, "01=男, 02=女")
    clarifications.answer_question("p1", q2, "学生基本信息表")

    doc = clarifications.build_rag_document("p1")
    assert "# 数据库管理员说明" in doc
    assert "## 表: `students`" in doc
    assert "### students.`gender`" in doc
    assert "### students.（整表）" in doc
    assert "**编码映射表**（用于 SQL 查询）:" in doc
    assert "| `01` | 男 |" in doc
    assert "**管理员回答**: 01=男, 02=女" in doc
    assert "*相关样本值*: 01, 02" in doc


def test_clarification_rag_document_aggregates_all_profiles(clarifications):
    a = clarifications.add_question("p1", "students", "q1")
    b = clarifications.add_question("p2", "orders", "q2")
    clarifications.answer_question("p1", a, "学生表")
    clarifications.answer_question("p2", b, "订单表")

    doc = clarifications.build_rag_document()
    assert "## 表: `orders`" in doc
    assert "## 表: `students`" in doc
    # 单 profile 视图只含自己的表
    assert "orders" not in clarifications.build_rag_document("p1")


def test_same_question_id_can_exist_in_two_profiles(clarifications):
    """主键是 (profile_id, question_id)：跨 profile 撞 id 不能丢数据。"""
    clarifications.upsert(
        Clarification(question_id="dup", profile_id="p1", table_name="t", answer="a1")
    )
    clarifications.upsert(
        Clarification(question_id="dup", profile_id="p2", table_name="t", answer="a2")
    )

    assert clarifications.count() == 2
    assert clarifications.get("p1", "dup").answer == "a1"
    assert clarifications.get("p2", "dup").answer == "a2"


def test_clarification_upsert_is_idempotent(clarifications):
    entry = Clarification(
        question_id="q1", profile_id="p1", table_name="t", answer="a", created_at="2026-01-01 00:00:00"
    )
    clarifications.upsert(entry)
    clarifications.upsert(entry)
    assert clarifications.count() == 1
    assert clarifications.get("p1", "q1").created_at == "2026-01-01 00:00:00"


# ---- explorations ----


def test_exploration_save_get_and_overwrite(explorations):
    assert explorations.is_explored("p1") is False
    assert explorations.report("p1") == ""
    assert explorations.get("p1") is None

    explorations.save("p1", "第一版报告", [{"table": "students", "column": "gender"}])
    assert explorations.is_explored("p1") is True
    assert explorations.report("p1") == "第一版报告"
    assert explorations.candidates("p1") == [{"table": "students", "column": "gender"}]
    assert explorations.get("p1").created_at

    explorations.save("p1", "第二版报告")
    assert explorations.count() == 1
    assert explorations.report("p1") == "第二版报告"
    assert explorations.candidates("p1") == []


def test_exploration_empty_report_still_counts_as_explored(explorations):
    """与历史 `_is_explored()` 一致：有记录就算探索过。"""
    explorations.save("p1", "")
    assert explorations.is_explored("p1") is True


def test_exploration_list_and_delete(explorations):
    explorations.save("p1", "r1", created_at="2026-01-01 00:00:00")
    explorations.save("p2", "r2", created_at="2026-02-01 00:00:00")

    assert [e.profile_id for e in explorations.list_all()] == ["p2", "p1"]
    assert explorations.delete("p1") is True
    assert explorations.delete("p1") is False
    assert explorations.count() == 1


# ---- importer ----


def write_legacy_data(root, *, profiles=2, clarifications=3, explorations=2):
    root.mkdir(parents=True, exist_ok=True)
    (root / "clarifications").mkdir(exist_ok=True)
    (root / "explorations").mkdir(exist_ok=True)

    profile_rows = [
        {
            "id": f"p{i}",
            "name": f"db{i}",
            "host": "127.0.0.1",
            "port": 3306 + i,
            "user": "root",
            "password": "pw",
            "database": f"db{i}",
            "db_type": "postgresql" if i == 1 else "mysql",
            "description": "",
        }
        for i in range(profiles)
    ]
    (root / "profiles.json").write_text(json.dumps(profile_rows), encoding="utf-8")

    entries = [
        {
            "question_id": f"q{i}",
            "table_name": "students",
            "column_name": f"c{i}",
            "question": f"问题 {i}",
            "sample_values": [str(i)],
            "answer": f"{i}=含义{i}",
            "status": "answered",
            "created_at": "2026-01-01 00:00:00",
            "answered_at": "2026-01-02 00:00:00",
        }
        for i in range(clarifications)
    ]
    # 条目故意不带 profile_id（老数据就是这样），靠文件名回填
    (root / "clarifications" / "p0.json").write_text(
        json.dumps(entries), encoding="utf-8"
    )

    for i in range(explorations):
        (root / "explorations" / f"p{i}.json").write_text(
            json.dumps(
                {
                    "profile_id": f"p{i}",
                    "db_name": f"db{i}",
                    "explored_at": "2026-03-01 10:00:00",
                    "report": f"报告 {i}",
                }
            ),
            encoding="utf-8",
        )

    return {
        "profiles": len(profile_rows),
        "clarifications": len(entries),
        "explorations": explorations,
    }


def test_importer_migrates_all_three_stores(storage, tmp_path):
    legacy = tmp_path / "legacy"
    counts = write_legacy_data(legacy)

    report = import_legacy_json(storage, legacy_dir=legacy)

    assert report.profiles == counts["profiles"]
    assert report.clarifications == counts["clarifications"]
    assert report.explorations == counts["explorations"]
    assert report.total == sum(counts.values())
    assert str(counts["profiles"]) in report.summary()

    # 逐条验证：数量对上了，内容也要对
    assert sorted(p.id for p in ProfileRepository(storage).list_all()) == ["p0", "p1"]
    assert ProfileRepository(storage).get("p1").kind is DialectKind.POSTGRESQL

    repo = ClarificationRepository(storage)
    assert repo.count() == counts["clarifications"]
    entry = repo.get("p0", "q0")
    assert entry is not None
    assert entry.profile_id == "p0"  # 由文件名回填
    assert entry.answer == "0=含义0"
    assert entry.sample_values == ["0"]

    assert ExplorationRepository(storage).report("p1") == "报告 1"


def test_importer_is_idempotent(storage, tmp_path):
    legacy = tmp_path / "legacy"
    counts = write_legacy_data(legacy)

    import_legacy_json(storage, legacy_dir=legacy)
    second = import_legacy_json(storage, legacy_dir=legacy)

    assert second.clarifications == counts["clarifications"]
    assert ClarificationRepository(storage).count() == counts["clarifications"]
    assert ProfileRepository(storage).count() == counts["profiles"]


def test_importer_raises_when_a_store_loses_records(storage, tmp_path, monkeypatch):
    """部分迁移必须报错 —— 静默丢问答会直接降低答案质量。"""
    legacy = tmp_path / "legacy"
    write_legacy_data(legacy, clarifications=3)

    real_upsert = ClarificationRepository.upsert

    def dropping_upsert(self, entry) -> None:
        if entry.question_id == "q1":
            return  # 模拟"某一条没写进去"
        real_upsert(self, entry)

    monkeypatch.setattr(ClarificationRepository, "upsert", dropping_upsert)

    with pytest.raises(MigrationError) as excinfo:
        import_legacy_json(storage, legacy_dir=legacy)

    message = str(excinfo.value)
    assert "clarifications" in message
    assert "源 3 条" in message
    assert "目标 2 条" in message


def test_importer_tolerates_missing_legacy_files(storage, tmp_path):
    """旧数据不存在时不能报错，只是迁移 0 条。"""
    report = import_legacy_json(storage, legacy_dir=tmp_path / "nope")
    assert report.total == 0


def test_importer_raises_on_corrupt_source(storage, tmp_path):
    legacy = tmp_path / "legacy"
    write_legacy_data(legacy)
    (legacy / "clarifications" / "p9.json").write_text("{ 这不是 JSON", encoding="utf-8")

    with pytest.raises(MigrationError):
        import_legacy_json(storage, legacy_dir=legacy)


# ---- 异步孪生 ----


async def test_repository_async_twins_are_coroutine_functions(conversations, clarifications):
    import inspect

    for repo, name in (
        (conversations, "acreate"),
        (conversations, "astore_message"),
        (conversations, "ahistory"),
        (clarifications, "aadd_question"),
        (clarifications, "aget_exploration_progress"),
    ):
        assert inspect.iscoroutinefunction(getattr(repo, name)), f"{name} 必须是协程函数"


async def test_repository_async_twins_work_inside_event_loop(storage):
    """在事件循环里跑一遍完整链路（证明 DB 访问被卸载到线程池）。"""
    convo_repo = ConversationRepository(storage)
    clar_repo = ClarificationRepository(storage)
    expl_repo = ExplorationRepository(storage)

    convo = await convo_repo.acreate("p1")
    await convo_repo.astore_message(convo.id, "user", "有多少学生？")
    assert await convo_repo.amessage_count(convo.id) == 1
    assert [m["content"] for m in await convo_repo.ahistory(convo.id)] == ["有多少学生？"]

    question_id = await clar_repo.aadd_question("p1", "students", "q?")
    assert await clar_repo.aanswer_question("p1", question_id, "01=男")
    assert await clar_repo.acount("p1") == 1
    assert await clar_repo.aget_clarification("p1", "students")

    await expl_repo.asave("p1", "报告")
    assert await expl_repo.ais_explored("p1") is True
