"""应用配置。

所有可调参数集中在这里，通过 `.env` 覆盖。历史实现把超时/上限散落在
orchestrator、tools、connection 各处，调整一个超时要翻五个文件。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dbchat.domain.profile import DatabaseProfile, DialectKind, parse_dialect_kind

#: 项目根目录（src/dbchat/config.py → 上溯三级）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """从环境变量 / .env 读取的配置。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 路径 ----
    project_root: Path = PROJECT_ROOT
    #: 运行时状态目录（SQLite、schema 缓存、图表产物）。已 gitignore。
    var_dir: Path = PROJECT_ROOT / "var"
    #: 前端构建产物目录。开发模式下由 Vite dev server 提供，可不存在。
    web_dist_dir: Path = PROJECT_ROOT / "web" / "dist"

    # ---- 默认数据库（向后兼容 .env 里的 DB_* 变量）----
    db_host: str = "127.0.0.1"
    db_port: int = 3307
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "training_exam_db"
    db_type: str = "mysql"

    # ---- LLM ----
    llm_provider: str = ""  # 留空则按 api_key 前缀自动判定
    llm_api_key: str = ""
    llm_api_base: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o"
    llm_max_tokens: int = 2048
    llm_temperature: float = 0.1
    llm_max_retries: int = 3
    anthropic_api_key: str = ""

    # ---- 超时（秒）----
    llm_timeout_s: int = 120
    llm_connect_timeout_s: int = 30
    db_connect_timeout_s: int = 30
    db_query_timeout_s: int = 60
    sandbox_timeout_s: int = 15
    #: 单次提问的总预算。超出后主动收尾并返回已有结果。
    run_timeout_s: int = 300
    #: 首次 schema 加载的预算（大库可能很慢）。
    schema_load_timeout_s: int = 180

    # ---- 缓存 ----
    schema_cache_ttl_s: int = 300
    #: 表数量超过此值进入大库模式（精简提示词、按需加载样本）。
    large_db_table_threshold: int = 20
    #: 进入 schema 缓存的抽样行数。
    sample_row_limit: int = 3

    # ---- 迭代上限 ----
    max_query_rounds: int = 5
    max_analysis_iterations: int = 6
    max_sql_iterations: int = 3
    max_compute_iterations: int = 1
    max_visualization_iterations: int = 2
    #: 单次查询返回给 LLM 的最大行数。
    query_row_limit: int = 200

    # ---- 服务 ----
    host: str = "0.0.0.0"
    port: int = 8000
    #: 开发模式：CORS 放开、返回更详细的错误、由 Vite dev server 提供前端。
    debug: bool = False

    # ---- 展示策略 ----
    #: 是否把原始 SQL 作为可展开项发给前端。默认关闭 —— 用户要的是答案，
    #: 不是 SQL；需要排查时再打开。
    expose_sql_to_user: bool = False
    #: 是否把模型思考过程流式展示。默认开启 —— 长耗时任务需要让用户看到进展。
    expose_thinking: bool = True

    # ---- RAG ----
    rag_top_k: int = 5
    rag_chunk_size: int = 800
    rag_chunk_overlap: int = 100

    @field_validator("var_dir", "web_dist_dir", mode="after")
    @classmethod
    def _resolve(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v)

    # ---- 派生路径 ----
    @property
    def db_path(self) -> Path:
        return self.var_dir / "dbchat.sqlite3"

    @property
    def schema_cache_dir(self) -> Path:
        return self.var_dir / "schema"

    @property
    def artifacts_dir(self) -> Path:
        return self.var_dir / "artifacts"

    @property
    def rag_dir(self) -> Path:
        return self.var_dir / "rag"

    @property
    def profiles_file(self) -> Path:
        return self.var_dir / "profiles.json"

    # ---- LLM 判定 ----
    @property
    def effective_api_key(self) -> str:
        return self.llm_api_key or self.anthropic_api_key

    @property
    def effective_provider(self) -> str:
        """provider 判定。显式配置优先，否则按 key 前缀推断。"""
        explicit = (self.llm_provider or "").strip().lower()
        if explicit in ("openai", "anthropic"):
            return explicit
        if self.anthropic_api_key and not self.llm_api_key:
            return "anthropic"
        if self.llm_api_key.startswith("sk-ant-"):
            return "anthropic"
        return "openai"

    @property
    def llm_available(self) -> bool:
        return bool(self.effective_api_key)

    def ensure_dirs(self) -> None:
        """创建运行时目录。"""
        for path in (self.var_dir, self.schema_cache_dir, self.artifacts_dir, self.rag_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ---- 默认连接 ----
    def default_profile(self) -> DatabaseProfile:
        """从 DB_* 变量构造默认连接配置。"""
        return DatabaseProfile(
            id="default",
            name=self.db_name,
            host=self.db_host,
            port=self.db_port,
            user=self.db_user,
            password=self.db_password,
            database=self.db_name,
            kind=parse_dialect_kind(self.db_type),
            description="默认数据库（来自 .env）",
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()


def load_profiles(settings: Settings | None = None) -> list[DatabaseProfile]:
    """读取连接配置列表；文件不存在时用 .env 的默认配置初始化。"""
    s = settings or get_settings()
    path = s.profiles_file
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            profiles = [_profile_from_json(item) for item in raw]
            if profiles:
                return profiles
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            # 配置损坏时必须显式暴露，不能悄悄退回默认库 ——
            # 否则用户会以为在查 A 库，实际查的是 B 库。
            raise ValueError(f"连接配置文件损坏: {path}: {exc}") from exc

    default = s.default_profile()
    save_profiles([default], s)
    return [default]


def save_profiles(profiles: list[DatabaseProfile], settings: Settings | None = None) -> None:
    s = settings or get_settings()
    s.ensure_dirs()
    payload = [p.model_dump(mode="json") for p in profiles]
    tmp = s.profiles_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(s.profiles_file)  # 原子替换，避免写一半被读到


def _profile_from_json(data: dict) -> DatabaseProfile:
    """兼容历史字段名（历史配置用 db_type 而非 kind）。"""
    kind_raw = data.get("kind") or data.get("db_type") or DialectKind.MYSQL.value
    return DatabaseProfile(
        id=str(data["id"]),
        name=data.get("name", ""),
        host=data.get("host", "127.0.0.1"),
        port=int(data.get("port", 3306)),
        user=data.get("user", "root"),
        password=data.get("password", ""),
        database=data.get("database", ""),
        kind=parse_dialect_kind(str(kind_raw)),
        description=data.get("description", ""),
    )


__all__ = ["PROJECT_ROOT", "Settings", "get_settings", "load_profiles", "save_profiles"]
