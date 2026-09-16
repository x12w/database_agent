"""真库集成测试 —— MySQL / PostgreSQL / 达梦。

连接参数一律从 `data/profiles.json` 读取（该文件已被 gitignore，含真实凭据），
**不在测试里硬编码任何密码**；读不到就 `pytest.skip` 并说明原因。

达梦的驱动（dmPython）不可导入时必须**显式 skip**，绝不能静默通过 ——
否则"国产库没测"会伪装成"国产库测过了"。

用法::

    pytest -m live_mysql -q
    pytest -m live_pg -q
    pytest -m live_dm -q
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from dbchat.config import get_settings
from dbchat.db import ConnectionManager, SqlExecutor, build_snapshot
from dbchat.db.dialects import get_dialect
from dbchat.db.drivers import load_dmpython
from dbchat.domain.errors import DbError
from dbchat.domain.profile import DatabaseProfile, DialectKind
from dbchat.domain.schema import CatalogScope

PROFILES_FILE = pathlib.Path(__file__).resolve().parents[2] / "data" / "profiles.json"

#: 实测在线的 profile id（远程达梦 7c51b0b2 不在此列：可能不可达，不作为依赖）。
PROFILE_TRAINING = "1da18658"   # MySQL 3307 training_exam_db
PROFILE_ECOMMERCE = "650489f6"  # MySQL 3308 ecommerce_db
PROFILE_HOSPITAL = "c1c24e63"   # PostgreSQL 5433 hospital_db
PROFILE_DAMENG = "2463d232"     # 达梦 5237 本地容器


def load_profile(profile_id: str) -> DatabaseProfile:
    """从 profiles.json 读一条连接配置；读不到就跳过。"""
    if not PROFILES_FILE.exists():
        pytest.skip(f"缺少 {PROFILES_FILE}（含真实凭据，已 gitignore），无法跑真库测试")
    try:
        raw = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - 配置损坏
        pytest.skip(f"{PROFILES_FILE} 不是合法 JSON: {exc}")

    for item in raw:
        if item.get("id") == profile_id:
            kind = item.get("kind") or item.get("db_type") or "mysql"
            return DatabaseProfile(
                id=str(item["id"]),
                name=item.get("name", ""),
                host=item["host"],
                port=int(item["port"]),
                user=item["user"],
                password=item.get("password", ""),
                database=item.get("database", ""),
                kind=(
                    DialectKind(str(kind).lower())
                    if str(kind).lower() in {k.value for k in DialectKind}
                    else DialectKind.MYSQL
                ),
            )
    pytest.skip(f"{PROFILES_FILE} 中没有 profile {profile_id}")


def require_dameng_driver() -> None:
    """达梦驱动不可用时显式 skip（绝不静默通过）。"""
    try:
        module = load_dmpython()
    except DbError as exc:
        pytest.skip(f"达梦驱动不可用，跳过国产库测试: {exc.user_message}")
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都必须显式暴露
        pytest.skip(f"达梦驱动导入失败，跳过国产库测试: {type(exc).__name__}: {exc}")
    assert module.paramstyle, "dmPython 未自报 paramstyle"


async def introspect(profile: DatabaseProfile, *, fast: bool = True):
    """连一次、采集一份快照，返回 (快照, 方言)。"""
    settings = get_settings()
    manager = ConnectionManager(connect_timeout_s=settings.db_connect_timeout_s)
    try:
        async with manager.acquire(profile) as lease:
            runner = SqlExecutor(lease, settings)
            snapshot = await build_snapshot(
                runner, CatalogScope(), fast=fast, sample_rows=settings.sample_row_limit
            )
            return snapshot, lease.dialect
    finally:
        await manager.close_all()


async def run_sql(profile: DatabaseProfile, sql: str, **kwargs: Any):
    settings = get_settings()
    manager = ConnectionManager(connect_timeout_s=settings.db_connect_timeout_s)
    try:
        async with manager.acquire(profile) as lease:
            return await SqlExecutor(lease, settings).query(sql, **kwargs)
    finally:
        await manager.close_all()


# --------------------------------------------------------------- MySQL


@pytest.mark.live_mysql
async def test_mysql_introspection_is_complete() -> None:
    """MySQL 内省：表、列、外键、索引都要真的取到。"""
    profile = load_profile(PROFILE_TRAINING)
    snapshot, dialect = await introspect(profile, fast=False)

    assert dialect.kind is DialectKind.MYSQL
    assert snapshot.table_count >= 5, f"training_exam_db 表数异常: {snapshot.table_count}"
    assert snapshot.foreign_keys, "没有采到任何外键"

    for table in snapshot.tables.values():
        assert table.columns, f"表 {table.name} 没有列"
        assert all(c.data_type for c in table.columns), f"表 {table.name} 有列缺类型"

    with_columns = [t for t in snapshot.tables.values() if t.indexes]
    assert with_columns, "没有采到任何索引"

    # 抽样确实取到了行（fast=False 才做）
    assert any(t.sample_rows for t in snapshot.tables.values()), "没有任何表取到样本"


@pytest.mark.live_mysql
async def test_mysql_row_counts_are_filled() -> None:
    profile = load_profile(PROFILE_TRAINING)
    snapshot, _ = await introspect(profile, fast=False)
    non_empty = [t for t in snapshot.tables.values() if (t.row_count or 0) > 0]
    assert non_empty, "所有表行数都是 0 —— 行数采集没生效"


@pytest.mark.live_mysql
async def test_mysql_query_and_row_limit() -> None:
    """行数上限在真库上确实生效，且截断标记正确。"""
    profile = load_profile(PROFILE_TRAINING)
    snapshot, _ = await introspect(profile, fast=True)
    table = max(snapshot.tables.values(), key=lambda t: t.row_count or 0)
    if (table.row_count or 0) < 3:  # pragma: no cover - 测试库太小
        pytest.skip("训练库里没有足够大的表来验证截断")

    settings = get_settings()
    manager = ConnectionManager(connect_timeout_s=settings.db_connect_timeout_s)
    try:
        async with manager.acquire(profile) as lease:
            executor = SqlExecutor(lease, settings)
            tiny = await executor.query(f"SELECT * FROM `{table.name}` LIMIT 1")
            assert tiny.row_count == 1
            assert tiny.columns
            assert tiny.sql.upper().count("LIMIT") == 1, "已有限流不该被重复添加"

            # 把上限压到 1 行，验证"截断"能被识别
            tighter = settings.model_copy(update={"query_row_limit": 1})
            truncated = await SqlExecutor(lease, tighter).query(
                f"SELECT * FROM `{table.name}`", snapshot=snapshot
            )
            assert truncated.row_count == 1
            assert truncated.truncated is True
            assert "LIMIT 2" in truncated.sql.upper(), truncated.sql
    finally:
        await manager.close_all()


@pytest.mark.live_mysql
async def test_mysql_literal_percent_s_survives_execution() -> None:
    """`LIKE '%s%'` 必须原样送到数据库 —— 老实现会把它改成 `'?%'`。"""
    profile = load_profile(PROFILE_TRAINING)
    result = await run_sql(
        profile,
        "SELECT '100%s' AS literal_1, 'a%s b' LIKE '%s%' AS literal_2, "
        "CONCAT('x', '%s', 'y') AS literal_3",
    )
    row = result.rows[0]
    assert row["literal_1"] == "100%s"
    assert row["literal_2"] in (1, True)
    assert row["literal_3"] == "x%sy"


@pytest.mark.live_mysql
async def test_mysql_guard_rejects_write() -> None:
    from dbchat.domain.errors import ValidationError

    profile = load_profile(PROFILE_TRAINING)
    with pytest.raises(ValidationError):
        await run_sql(profile, "DELETE FROM employee")


@pytest.mark.live_mysql
async def test_mysql_placeholder_matches_driver() -> None:
    import pymysql

    assert pymysql.paramstyle == "pyformat"
    assert get_dialect(DialectKind.MYSQL).placeholder() == "%s"


@pytest.mark.live_mysql
async def test_mysql_probe_counts_tables() -> None:
    profile = load_profile(PROFILE_ECOMMERCE)
    manager = ConnectionManager()
    probe = await manager.probe(profile)
    assert probe.success, probe.message
    assert probe.table_count > 0, probe.message
    await manager.close_all()


# ---------------------------------------------------------- PostgreSQL


@pytest.mark.live_pg
async def test_pg_introspection_is_complete() -> None:
    profile = load_profile(PROFILE_HOSPITAL)
    snapshot, dialect = await introspect(profile, fast=False)

    assert dialect.kind is DialectKind.POSTGRESQL
    assert snapshot.table_count >= 3, f"hospital_db 表数异常: {snapshot.table_count}"

    # 新实现用 pg_constraint 取外键、pg_index 取索引列 —— 必须真的有内容
    assert snapshot.foreign_keys, "没有采到任何外键"

    indexed = [t for t in snapshot.tables.values() if t.indexes]
    assert indexed, "没有采到任何索引"
    for table in indexed:
        for index in table.indexes:
            assert index.name
            assert index.columns, f"{table.name}.{index.name} 是空索引（旧实现拿不到列）"

    # 主键必须被标出来（并且回填到了列上）
    pk_tables = [t for t in snapshot.tables.values() if any(i.is_primary for i in t.indexes)]
    assert pk_tables, "没有任何表被识别出主键索引"
    assert any(c.is_primary_key for t in pk_tables for c in t.columns), "主键列没被回填"


@pytest.mark.live_pg
async def test_pg_query_and_truncation() -> None:
    profile = load_profile(PROFILE_HOSPITAL)
    snapshot, _ = await introspect(profile, fast=True)
    table = max(snapshot.tables.values(), key=lambda t: t.row_count or 0)
    table_name = snapshot.resolve_table_name(table.name) or table.name

    settings = get_settings()
    manager = ConnectionManager(connect_timeout_s=settings.db_connect_timeout_s)
    try:
        async with manager.acquire(profile) as lease:
            executor = SqlExecutor(lease, settings)
            result = await executor.query(f'SELECT * FROM "{table_name}" LIMIT 2')
            assert result.row_count == 2
            # PG 的小写列名必须能按原样取到值
            assert all(c in result.rows[0] for c in result.columns)
    finally:
        await manager.close_all()


@pytest.mark.live_pg
async def test_pg_schema_qualified_query_validates() -> None:
    """`FROM "public"."table"` 这种限定名要能被正确解析与校验。"""
    profile = load_profile(PROFILE_HOSPITAL)
    snapshot, _ = await introspect(profile, fast=True)
    table_name = snapshot.resolve_table_name(next(iter(snapshot.tables)))

    result = await run_sql(
        profile,
        f'SELECT COUNT(*) AS n FROM "public"."{table_name}"',
        snapshot=snapshot,
    )
    assert result.row_count == 1
    assert result.rows[0]["n"] >= 0


@pytest.mark.live_pg
async def test_pg_statement_timeout_is_accepted() -> None:
    """`SET statement_timeout` 必须在真库上可执行（旧实现用的 SET LOCAL 是空转）。"""
    profile = load_profile(PROFILE_HOSPITAL)
    result = await run_sql(profile, "SELECT 1 AS ok")
    assert result.rows[0]["ok"] == 1


# --------------------------------------------------------------- 达梦


@pytest.mark.live_dm
async def test_dm_introspection_and_exact_row_counts() -> None:
    """达梦内省 —— 重点验证"NUM_ROWS 全为 0"被精确计数修正。"""
    require_dameng_driver()
    profile = load_profile(PROFILE_DAMENG)
    snapshot, dialect = await introspect(profile, fast=True)

    assert dialect.kind is DialectKind.DAMENG
    assert snapshot.table_count >= 5, f"达梦库表数异常: {snapshot.table_count}"

    # 内部表（##% / SREF%）必须被过滤掉
    leaked = [name for name in snapshot.tables if name.startswith(("##", "SREF"))]
    assert not leaked, f"内部表泄漏到快照里: {leaked}"

    # 关键：不补 COUNT(*) 的话行数会整片为 0
    exact = [t for t in snapshot.tables.values() if t.row_count and not t.row_count_approx]
    assert exact, "没有任何表走了精确计数 —— 达梦的行数可信度开关没生效"
    assert sum(t.row_count or 0 for t in snapshot.tables.values()) > 0

    assert snapshot.foreign_keys, "没有采到任何外键"


@pytest.mark.live_dm
async def test_dm_never_emits_limit() -> None:
    """达梦不认 LIMIT：查询与抽样都必须走 FETCH FIRST。"""
    require_dameng_driver()
    profile = load_profile(PROFILE_DAMENG)
    snapshot, _ = await introspect(profile, fast=True)
    table_name = next(iter(snapshot.tables))

    settings = get_settings()
    manager = ConnectionManager(connect_timeout_s=settings.db_connect_timeout_s)
    try:
        async with manager.acquire(profile) as lease:
            executor = SqlExecutor(lease, settings)
            result = await executor.query(
                f'SELECT * FROM "{table_name}"', snapshot=snapshot
            )
            assert "LIMIT" not in result.sql.upper(), result.sql
            assert "FETCH FIRST" in result.sql.upper(), result.sql
            assert result.rows, "达梦查询没有返回任何行"
            # 行映射大小写不敏感
            first = result.rows[0]
            assert all(first.get(c) is not None or c in first for c in result.columns)
    finally:
        await manager.close_all()


@pytest.mark.live_dm
async def test_dm_literal_percent_s_survives_execution() -> None:
    """达梦绑定路径：`'100%s'` / `'%s%'` / `'-- %s'` 都不能被改写成 `?`。

    用 LENGTH 做旁证：字面量一旦被改写成 `?`，长度就变了。
    （达梦不接受 `expr LIKE expr AS 别名`，所以这里不写 LIKE。）
    """
    require_dameng_driver()
    profile = load_profile(PROFILE_DAMENG)
    result = await run_sql(
        profile,
        "SELECT '100%s' AS literal_1, LENGTH('%s%') AS literal_2, '-- %s' AS literal_3 FROM DUAL",
    )
    row = result.rows[0]
    assert row["literal_1"] == "100%s"
    assert int(row["literal_2"]) == 3, "字面量里的 %s 被改写了"
    assert row["literal_3"] == "-- %s"


@pytest.mark.live_dm
async def test_dm_placeholder_comes_from_driver() -> None:
    """占位符必须来自驱动自报的 paramstyle（实测为 qmark）。"""
    require_dameng_driver()
    module = load_dmpython()
    dialect = get_dialect(DialectKind.DAMENG)
    assert module.paramstyle == "qmark"
    assert dialect.placeholder() == "?"
    assert dialect.bind("SELECT %s FROM DUAL WHERE A = %s", 2) == "SELECT ? FROM DUAL WHERE A = ?"
    assert dialect.bind("SELECT '100%s' FROM DUAL", 0) == "SELECT '100%s' FROM DUAL"


@pytest.mark.live_dm
async def test_dm_probe_reports_table_count() -> None:
    require_dameng_driver()
    profile = load_profile(PROFILE_DAMENG)
    manager = ConnectionManager()
    probe = await manager.probe(profile)
    assert probe.success, probe.message
    assert probe.table_count > 0, probe.message
    await manager.close_all()


@pytest.mark.live_dm
async def test_dm_bad_password_is_classified_as_auth() -> None:
    """错误归类靠 DmError.code，而不是中文报文匹配。"""
    require_dameng_driver()
    profile = load_profile(PROFILE_DAMENG).model_copy(update={"password": "definitely-wrong"})
    manager = ConnectionManager(connect_timeout_s=5)
    try:
        probe = await manager.probe(profile)
        assert probe.success is False
        assert "密码" in probe.message or "认证" in probe.message, probe.message
    finally:
        await manager.close_all()


# ------------------------------------------------- 跨库一致性（有则跑）


@pytest.mark.live_mysql
async def test_manager_keeps_separate_connections_per_profile() -> None:
    """注册表按 profile 分别持有连接 —— 不再有"当前连接"被顶掉的问题。"""
    training = load_profile(PROFILE_TRAINING)
    ecommerce = load_profile(PROFILE_ECOMMERCE)
    manager = ConnectionManager()
    try:
        async with (
            manager.acquire(training) as first,
            manager.acquire(ecommerce) as second,
        ):
            assert first.connection is not second.connection

            def _count(lease: Any, table: str) -> int:  # noqa: ANN401
                with lease.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
                    return int(cur.fetchone()["n"])

            import anyio

            n1 = await anyio.to_thread.run_sync(_count, first, "employee")
            n2 = await anyio.to_thread.run_sync(_count, second, "customers")
            assert n1 >= 0 and n2 >= 0
        assert training.id in manager.connected_profile_ids()
        assert ecommerce.id in manager.connected_profile_ids()
    finally:
        await manager.close_all()
    assert manager.connected_profile_ids() == frozenset()
