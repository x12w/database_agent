"""Application configuration constants — loaded from .env file."""

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Load .env file from project root
from dotenv import load_dotenv
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)


@dataclass
class DatabaseProfile:
    """Connection profile for a MySQL database."""
    id: str = ""              # unique id, auto-generated
    name: str = ""            # user-friendly display name
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""        # database name to USE
    db_type: str = "mysql"    # mysql, postgresql, sqlite, etc.
    description: str = ""     # optional notes

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "db_type": self.db_type,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DatabaseProfile":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            host=d.get("host", "127.0.0.1"),
            port=d.get("port", 3306),
            user=d.get("user", "root"),
            password=d.get("password", ""),
            database=d.get("database", ""),
            db_type=d.get("db_type", "mysql"),
            description=d.get("description", ""),
        )


@dataclass
class Config:
    # ---- MySQL connection (default, backward-compatible) ----
    DB_HOST: str = os.getenv("DB_HOST", "127.0.0.1")
    DB_PORT: int = int(os.getenv("DB_PORT", "3307"))
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "training123")
    DB_NAME: str = os.getenv("DB_NAME", "training_exam_db")
    DB_CONNECT_TIMEOUT: int = 30
    DB_QUERY_TIMEOUT: int = 60

    # ---- LLM ----
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "").lower()
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_API_BASE: str = os.getenv("LLM_API_BASE", "https://api.openai.com/v1")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "gpt-4o")
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    LLM_MAX_RETRIES: int = 3

    @property
    def effective_api_key(self) -> str:
        if self.LLM_API_KEY:
            return self.LLM_API_KEY
        return os.getenv("ANTHROPIC_API_KEY", "")

    @property
    def effective_provider(self) -> str:
        if self.LLM_PROVIDER in ("openai", "anthropic"):
            return self.LLM_PROVIDER
        if os.getenv("ANTHROPIC_API_KEY", ""):
            return "anthropic"
        if self.LLM_API_KEY.startswith("sk-ant-"):
            return "anthropic"
        return "openai"

    # ---- RAG ----
    RAG_DOC_PATH: str = "training_exam_db_rag.md"
    RAG_STRUCTURE_DOC_PATH: str = "training_exam_db_structure.md"
    RAG_CHUNK_SIZE: int = 800
    RAG_CHUNK_OVERLAP: int = 100
    RAG_TOP_K: int = 5
    CLARIFICATIONS_PATH: str = "data/clarifications"

    # ---- Schema cache ----
    CACHE_TTL_SECONDS: int = 300

    # ---- Server ----
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    ADMIN_HOST: str = "0.0.0.0"
    ADMIN_PORT: int = 8001

    # ---- Limits ----
    MAX_QUERIES_PER_MESSAGE: int = 3
    MAX_MESSAGE_LENGTH: int = 4000

    # ---- Database Profiles ----
    PROFILES_FILE: str = "data/profiles.json"

    def _profiles_path(self) -> Path:
        return Path(__file__).parent.parent / self.PROFILES_FILE

    def load_profiles(self) -> list[DatabaseProfile]:
        """Load database profiles from disk. Falls back to default from .env."""
        path = self._profiles_path()
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                profiles = [DatabaseProfile.from_dict(d) for d in data]
                if profiles:
                    return profiles
            except (json.JSONDecodeError, KeyError):
                pass

        # No profiles file or empty — create default from .env
        default = self.get_default_profile()
        self.save_profiles([default])
        return [default]

    def save_profiles(self, profiles: list[DatabaseProfile]):
        """Save database profiles to disk."""
        path = self._profiles_path()
        os.makedirs(path.parent, exist_ok=True)
        data = [p.to_dict() for p in profiles]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_default_profile(self) -> DatabaseProfile:
        """Build a DatabaseProfile from the legacy DB_* env vars."""
        return DatabaseProfile(
            id="default",
            name=self.DB_NAME,
            host=self.DB_HOST,
            port=self.DB_PORT,
            user=self.DB_USER,
            password=self.DB_PASSWORD,
            database=self.DB_NAME,
            description="默认数据库（来自 .env 配置）",
        )


config = Config()
