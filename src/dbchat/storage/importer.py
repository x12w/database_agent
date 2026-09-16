"""一次性迁移：旧 JSON 文件 → SQLite。

迁移面：
    data/profiles.json            → profiles
    data/clarifications/*.json    → clarifications
    data/explorations/*.json      → explorations

**每个 store 都做条数断言**：源里有多少条，库里就必须能逐条查回来，否则
抛 `MigrationError`。部分迁移是最危险的结果 —— 少掉的那几条管理员问答
不会报错，只会让 agent 在后续提问里给出错误答案，而且没人知道为什么。

迁移是幂等的（全部 upsert），可以重复执行。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbchat.config import Settings, get_settings
from dbchat.domain.errors import DbChatError
from dbchat.domain.profile import DatabaseProfile, DialectKind, parse_dialect_kind
from dbchat.storage.clarifications import Clarification, ClarificationRepository
from dbchat.storage.db import Storage, get_storage
from dbchat.storage.explorations import ExplorationRepository
from dbchat.storage.profiles import ProfileRepository

logger = logging.getLogger(__name__)

#: 旧数据所在目录名（相对项目根）。
LEGACY_DATA_DIRNAME = "data"


class MigrationError(DbChatError):
    """迁移结果与源数据不一致。"""

    code = "migration"


@dataclass(slots=True, frozen=True)
class ImportReport:
    """迁移结果统计。"""

    profiles: int = 0
    clarifications: int = 0
    explorations: int = 0

    @property
    def total(self) -> int:
        return self.profiles + self.clarifications + self.explorations

    def summary(self) -> str:
        return (
            f"迁移完成：连接配置 {self.profiles} 条，"
            f"管理员问答 {self.clarifications} 条，"
            f"探索报告 {self.explorations} 份，"
            f"合计 {self.total} 条"
        )


def import_legacy_json(
    storage: Storage | None = None,
    *,
    settings: Settings | None = None,
    legacy_dir: str | Path | None = None,
    profiles_file: str | Path | None = None,
    clarifications_dir: str | Path | None = None,
    explorations_dir: str | Path | None = None,
) -> ImportReport:
    """把旧 JSON 数据导入 SQLite。

    Args:
        storage: 目标库；默认取进程内共享实例。
        legacy_dir: 旧数据根目录；默认 `Settings.project_root / "data"`。
        profiles_file / clarifications_dir / explorations_dir: 单独覆盖某一路径
            （测试与非常规部署用）。

    Returns:
        `ImportReport`。

    Raises:
        MigrationError: 任一 store 的源条数与库里可查回的条数不一致。
    """
    active_settings = settings or get_settings()
    db = storage or get_storage(active_settings)
    db.migrate()

    root = Path(legacy_dir) if legacy_dir is not None else active_settings.project_root / LEGACY_DATA_DIRNAME
    profiles_path = Path(profiles_file) if profiles_file is not None else root / "profiles.json"
    clar_dir = Path(clarifications_dir) if clarifications_dir is not None else root / "clarifications"
    expl_dir = Path(explorations_dir) if explorations_dir is not None else root / "explorations"

    profiles_count = _import_profiles(ProfileRepository(db), profiles_path)
    clarifications_count = _import_clarifications(ClarificationRepository(db), clar_dir)
    explorations_count = _import_explorations(ExplorationRepository(db), expl_dir)

    report = ImportReport(
        profiles=profiles_count,
        clarifications=clarifications_count,
        explorations=explorations_count,
    )
    logger.info(report.summary())
    return report


def _read_json(path: Path) -> Any:
    """读一个 JSON 文件；不存在返回 None，损坏则显式报错。"""
    if not path.exists():
        logger.info("跳过不存在的旧数据文件: %s", path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # 源数据损坏时绝不能"当作空"继续 —— 那会静默丢数据。
        raise MigrationError(f"旧数据文件损坏，无法解析: {path}", detail=str(exc), cause=exc) from exc


def _assert_counts(store: str, source: int, target: int, *, detail: str = "") -> None:
    """条数断言：源与目标必须一致。"""
    if source != target:
        raise MigrationError(
            f"{store} 迁移数量不一致：源 {source} 条，目标 {target} 条",
            detail=detail or f"缺失 {source - target} 条",
        )


def _import_profiles(repo: ProfileRepository, path: Path) -> int:
    """迁移连接配置。"""
    data = _read_json(path)
    if not data:
        return 0
    if not isinstance(data, list):
        raise MigrationError(f"连接配置文件格式错误（应为数组）: {path}")

    profiles = [_profile_from_legacy(item) for item in data if isinstance(item, dict)]
    for profile in profiles:
        repo.upsert(profile)

    found = sum(1 for profile in profiles if repo.get(profile.id) is not None)
    _assert_counts("profiles", len(profiles), found, detail=f"来源: {path}")
    return len(profiles)


def _profile_from_legacy(data: dict[str, Any]) -> DatabaseProfile:
    """旧 profiles.json 用的是 `db_type` 字段名。"""
    kind_raw = data.get("kind") or data.get("db_type") or DialectKind.MYSQL.value
    try:
        kind = parse_dialect_kind(str(kind_raw))
    except ValueError as exc:
        raise MigrationError(
            f"连接配置 {data.get('id')!r} 的数据库类型无法识别: {kind_raw!r}", detail=str(exc)
        ) from exc

    return DatabaseProfile(
        id=str(data.get("id") or ""),
        name=data.get("name", ""),
        host=data.get("host", "127.0.0.1"),
        port=int(data.get("port", 3306)),
        user=data.get("user", "root"),
        password=data.get("password", ""),
        database=data.get("database", ""),
        kind=kind,
        description=data.get("description", ""),
    )


def _import_clarifications(repo: ClarificationRepository, directory: Path) -> int:
    """迁移管理员问答。文件名即 profile_id，条目内缺 profile_id 时用文件名回填。"""
    if not directory.is_dir():
        logger.info("跳过不存在的问答目录: %s", directory)
        return 0

    sources: list[Clarification] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if not data:
            continue
        if not isinstance(data, list):
            raise MigrationError(f"问答文件格式错误（应为数组）: {path}")
        for item in data:
            if isinstance(item, dict):
                sources.append(Clarification.from_dict(item, profile_id=path.stem))

    for entry in sources:
        repo.upsert(entry)

    found = sum(
        1 for entry in sources if repo.get(entry.profile_id, entry.question_id) is not None
    )
    _assert_counts("clarifications", len(sources), found, detail=f"来源: {directory}")
    return len(sources)


def _import_explorations(repo: ExplorationRepository, directory: Path) -> int:
    """迁移探索报告。一个 profile 一份，文件名或文件内 profile_id 二选一。"""
    if not directory.is_dir():
        logger.info("跳过不存在的探索目录: %s", directory)
        return 0

    sources: list[tuple[str, str, Sequence[dict[str, Any]], str]] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if not data:
            continue
        if not isinstance(data, dict):
            raise MigrationError(f"探索报告格式错误（应为对象）: {path}")
        profile_id = str(data.get("profile_id") or path.stem)
        candidates = data.get("candidates") or []
        sources.append(
            (
                profile_id,
                str(data.get("report", "")),
                [item for item in candidates if isinstance(item, dict)],
                str(data.get("explored_at", "")),
            )
        )

    for profile_id, report, candidates, explored_at in sources:
        repo.save(profile_id, report, candidates, created_at=explored_at or None)

    found = sum(1 for profile_id, *_ in sources if repo.is_explored(profile_id))
    _assert_counts("explorations", len(sources), found, detail=f"来源: {directory}")
    return len(sources)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：`python -m dbchat.storage.importer [旧数据目录]`。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = list(sys.argv[1:] if argv is None else argv)
    legacy_dir = args[0] if args else None
    try:
        report = import_legacy_json(legacy_dir=legacy_dir)
    except MigrationError as exc:
        print(f"迁移失败：{exc}（{exc.detail}）", file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())


__all__ = [
    "LEGACY_DATA_DIRNAME",
    "ImportReport",
    "MigrationError",
    "import_legacy_json",
    "main",
]
