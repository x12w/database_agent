"""领域模型 —— 纯数据，无 IO，不依赖 FastAPI。"""

from dbchat.domain.errors import (
    DB_ERROR_MESSAGES,
    RETRYABLE_DB_ERRORS,
    ConfigError,
    DbChatError,
    DbError,
    DbErrorKind,
    LlmError,
    LlmUnavailableError,
    ToolError,
    ValidationError,
)
from dbchat.domain.profile import DatabaseProfile, DialectKind, parse_dialect_kind
from dbchat.domain.query import Artifact, BoundSQL, QueryResult, TypeFamily, rows_to_csv
from dbchat.domain.schema import (
    SCHEMA_FORMAT_VERSION,
    CatalogScope,
    ColumnMeta,
    ColumnValueType,
    ForeignKeyMeta,
    IndexMeta,
    SchemaSnapshot,
    TableMeta,
    scope_cache_key,
    stable_hash,
)

__all__ = [
    "DB_ERROR_MESSAGES",
    "RETRYABLE_DB_ERRORS",
    "SCHEMA_FORMAT_VERSION",
    "Artifact",
    "BoundSQL",
    "CatalogScope",
    "ColumnMeta",
    "ColumnValueType",
    "ConfigError",
    "DatabaseProfile",
    "DbChatError",
    "DbError",
    "DbErrorKind",
    "DialectKind",
    "ForeignKeyMeta",
    "IndexMeta",
    "LlmError",
    "LlmUnavailableError",
    "QueryResult",
    "SchemaSnapshot",
    "TableMeta",
    "ToolError",
    "TypeFamily",
    "ValidationError",
    "parse_dialect_kind",
    "rows_to_csv",
    "scope_cache_key",
    "stable_hash",
]
