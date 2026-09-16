"""HTTP 请求 / 响应模型。

字段名对齐前端的 `web/src/types/api.ts`（`db_type` 而不是领域层的 `kind`），
历史实现里同一份数据在 `app/main.py` 与 `app/admin_app.py` 有两套模型且口径
不一致（一个用宽松 dict、一个用 pydantic），这里只留一套。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ 健康检查


class HealthResponse(_Model):
    """`GET /api/health`。"""

    status: str = "ok"
    #: 是否有活连接（不再有"当前连接"这个概念，问的是"有没有连上的库"）。
    database: bool = False
    db_name: str | None = None
    llm_available: bool = False
    rag_ready: bool = False
    schema_tables: int = 0


# ------------------------------------------------------------------ 连接配置


class DatabaseProfileView(_Model):
    """连接配置的展示形态 —— 不含密码。"""

    id: str
    name: str = ""
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    database: str = ""
    db_type: str = "mysql"
    description: str = ""
    explored: bool = False
    connected: bool = False


class DatabasesResponse(_Model):
    profiles: list[DatabaseProfileView] = Field(default_factory=list)
    #: 默认库（配置列表的第一个）；没有配置时为 None。
    current: str | None = None


class ProfileCreate(_Model):
    """新增连接配置。`name`、`database` 至少填一个。"""

    name: str = ""
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""
    db_type: str = "mysql"
    description: str = ""


class ProfileUpdate(_Model):
    """修改连接配置 —— 全部可选，只改传了的字段。"""

    name: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    db_type: str | None = None
    description: str | None = None


class DatabaseMutationResponse(_Model):
    success: bool = True
    message: str = ""
    profile: DatabaseProfileView | None = None
    table_count: int = 0


class DatabaseStatusResponse(_Model):
    profile_id: str
    name: str = ""
    database: str = ""
    db_type: str = "mysql"
    explored: bool = False
    table_count: int = 0
    tables: list[str] = Field(default_factory=list)
    foreign_key_count: int = 0
    suggestions: list[str] = Field(default_factory=list)
    explored_at: str = ""
    report: str = ""


class DeleteResponse(_Model):
    success: bool = True
    message: str = ""


# --------------------------------------------------------------- 表与筛选


class TableInfo(_Model):
    name: str
    row_count: int = 0
    column_count: int = 0
    comment: str = ""


class TablesResponse(_Model):
    tables: list[TableInfo] = Field(default_factory=list)
    #: 取不到表清单时的原因（如连接失败）；正常时为空。
    message: str | None = None


class TableFilterRequest(_Model):
    """表筛选入参：`{"tables": [...]}` 或 `{"tables": null}`（不限）。"""

    tables: list[str] | None = None


class TableFilterResponse(_Model):
    success: bool = True
    message: str = ""
    table_filter: list[str] | None = None


# --------------------------------------------------------------- 管理员问答


class AdminQuestionView(_Model):
    question_id: str
    table_name: str
    column_name: str = ""
    question: str
    sample_values: list[str] = Field(default_factory=list)
    answer: str = ""
    status: str = "pending"
    created_at: str = ""
    answered_at: str = ""
    profile_id: str = ""


class AdminQuestionsResponse(_Model):
    questions: list[AdminQuestionView] = Field(default_factory=list)
    pending_count: int = 0
    total_count: int = 0
    profile_id: str = ""


class AnswerRequest(_Model):
    answer: str
    #: 不传时按 question_id 在所有 profile 里定位（见路由）。
    profile_id: str | None = None


class IgnoreRequest(_Model):
    profile_id: str | None = None


class AdminMutationResponse(_Model):
    success: bool = True
    question_id: str | None = None
    profile_id: str | None = None
    message: str = ""


__all__ = [
    "AdminMutationResponse",
    "AdminQuestionView",
    "AdminQuestionsResponse",
    "AnswerRequest",
    "DatabaseMutationResponse",
    "DatabaseProfileView",
    "DatabaseStatusResponse",
    "DatabasesResponse",
    "DeleteResponse",
    "HealthResponse",
    "IgnoreRequest",
    "ProfileCreate",
    "ProfileUpdate",
    "TableFilterRequest",
    "TableFilterResponse",
    "TableInfo",
    "TablesResponse",
]
