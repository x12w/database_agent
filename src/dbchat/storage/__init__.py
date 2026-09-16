"""SQLite 持久化层。

    db.py            连接、迁移、事务、异步卸载
    migrations/      版本化 DDL
    conversations.py 会话与消息
    profiles.py      数据库连接配置
    clarifications.py 管理员问答（管理员面板 + RAG 语料）
    explorations.py  探索报告
    importer.py      旧 JSON → SQLite 的一次性迁移

约定：每个仓储的同步方法是业务实现，`OffloadableRepo` 自动生成
`a` 前缀的异步孪生（在线程池里执行）。异步调用方**必须**用 `a` 版本，
否则 SQLite 的磁盘 IO 会卡住事件循环。

用法::

    from dbchat.storage import get_conversation_repository

    repo = get_conversation_repository()
    conv = await repo.acreate(profile_id)
    await repo.astore_message(conv.id, "user", "有多少学生？")
"""

from dbchat.storage.clarifications import (
    STATUS_ANSWERED,
    STATUS_IGNORED,
    STATUS_PENDING,
    Clarification,
    ClarificationRepository,
    extract_mappings,
    get_clarification_repository,
)
from dbchat.storage.conversations import (
    Conversation,
    ConversationDetail,
    ConversationRepository,
    StoredMessage,
    get_conversation_repository,
)
from dbchat.storage.db import (
    MIGRATIONS_DIR,
    OffloadableRepo,
    Storage,
    get_storage,
    reset_storage_cache,
    utc_now_str,
)
from dbchat.storage.explorations import (
    Exploration,
    ExplorationRepository,
    get_exploration_repository,
)
from dbchat.storage.profiles import ProfileRepository, get_profile_repository

#: 迁移相关的名字按需导入 —— 它们是**一次性**工具，不该被应用的每次
#: `import dbchat.storage` 拖着一起加载；顺带避免 `python -m
#: dbchat.storage.importer` 触发 runpy 的重复导入警告。
_LAZY_IMPORTER_EXPORTS = frozenset({"ImportReport", "MigrationError", "import_legacy_json"})


def __getattr__(name: str):
    if name in _LAZY_IMPORTER_EXPORTS:
        from dbchat.storage import importer

        return getattr(importer, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "MIGRATIONS_DIR",
    "STATUS_ANSWERED",
    "STATUS_IGNORED",
    "STATUS_PENDING",
    "Clarification",
    "ClarificationRepository",
    "Conversation",
    "ConversationDetail",
    "ConversationRepository",
    "Exploration",
    "ExplorationRepository",
    "ImportReport",
    "MigrationError",
    "OffloadableRepo",
    "ProfileRepository",
    "Storage",
    "StoredMessage",
    "extract_mappings",
    "get_clarification_repository",
    "get_conversation_repository",
    "get_exploration_repository",
    "get_profile_repository",
    "get_storage",
    "import_legacy_json",
    "reset_storage_cache",
    "utc_now_str",
]
