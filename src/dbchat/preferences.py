"""Desktop preferences: validated, redacted API views and atomic private storage."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from dbchat.config import Settings

# Each editable field has a stable key, human label, group and optional bounds.
_GROUPS = {
    "model": "模型服务",
    "timeouts": "超时与连接",
    "limits": "查询与计算",
    "knowledge": "知识检索",
    "display": "显示与存储",
}
_FIELDS = [
    ("llm_provider", "接口协议", "model", None, None),
    ("llm_api_base", "服务地址", "model", None, None),
    ("llm_model", "模型名称", "model", None, None),
    ("llm_api_key", "API 密钥", "model", None, None),
    ("anthropic_api_key", "备用 Anthropic 密钥", "model", None, None),
    ("llm_max_tokens", "最大输出 Token", "model", 16, 131072),
    ("llm_temperature", "温度", "model", 0, 2),
    ("llm_max_retries", "失败重试次数", "model", 0, 10),
    ("llm_timeout_s", "模型请求超时（秒）", "timeouts", 1, 3600),
    ("llm_connect_timeout_s", "模型连接超时（秒）", "timeouts", 1, 300),
    ("db_connect_timeout_s", "数据库连接超时（秒）", "timeouts", 1, 300),
    ("db_query_timeout_s", "数据库查询超时（秒）", "timeouts", 1, 3600),
    ("run_timeout_s", "一次提问总时限（秒）", "timeouts", 10, 7200),
    ("schema_load_timeout_s", "结构加载超时（秒）", "timeouts", 1, 3600),
    ("sandbox_timeout_s", "计算与绘图超时（秒）", "timeouts", 1, 300),
    ("query_row_limit", "查询返回行数", "limits", 1, 10000),
    ("max_query_rounds", "查询轮数", "limits", 1, 20),
    ("max_analysis_iterations", "分析迭代次数", "limits", 1, 20),
    ("max_sql_iterations", "SQL 迭代次数", "limits", 1, 20),
    ("max_compute_iterations", "计算迭代次数", "limits", 1, 20),
    ("max_visualization_iterations", "绘图迭代次数", "limits", 1, 20),
    ("schema_cache_ttl_s", "结构缓存有效期（秒）", "limits", 0, 86400),
    ("large_db_table_threshold", "大库模式表数阈值", "limits", 1, 10000),
    ("sample_row_limit", "每表样本行数", "limits", 0, 100),
    ("rag_top_k", "检索片段数", "knowledge", 1, 30),
    ("rag_chunk_size", "片段长度", "knowledge", 100, 10000),
    ("rag_chunk_overlap", "相邻片段重叠长度", "knowledge", 0, 2000),
    ("rag_build_timeout_s", "索引构建超时（秒）", "knowledge", 1, 3600),
    ("rag_doc_path", "业务知识文件（Markdown）", "knowledge", None, None),
    ("rag_structure_doc_path", "结构说明文件（Markdown）", "knowledge", None, None),
    ("expose_sql_to_user", "显示可展开的 SQL", "display", None, None),
    ("expose_thinking", "显示模型思考过程", "display", None, None),
    ("sqlite_busy_timeout_ms", "本地存储锁等待（毫秒）", "display", 100, 60000),
]
SECRETS = frozenset({"llm_api_key", "anthropic_api_key"})
_LOCK = threading.Lock()


def fields() -> list[dict]:
    result = []
    for key, label, group, minimum, maximum in _FIELDS:
        default = Settings.model_fields[key].default
        kind = (
            "secret"
            if key in SECRETS
            else "boolean"
            if isinstance(default, bool)
            else "number"
            if isinstance(default, (float, int))
            else "text"
        )
        item = dict(key=key, label=label, group=group, type=kind, min=minimum, max=maximum)
        if key == "llm_provider":
            item.update(
                type="select",
                choices=[
                    {"value": "", "label": "自动识别"},
                    {"value": "openai", "label": "OpenAI 兼容"},
                    {"value": "anthropic", "label": "Anthropic"},
                ],
            )
        item["step"] = 0.1 if isinstance(default, float) else 1
        result.append(item)
    return result


def validate(values: dict) -> dict:
    """Reject unknown/type-confused fields without echoing credentials in errors."""
    specs = {f["key"]: f for f in fields()}
    for key, value in values.items():
        if key not in specs:
            raise ValueError("包含不支持的设置项")
        spec = specs[key]
        label = spec["label"]
        if spec["type"] == "boolean":
            valid = type(value) is bool
        elif spec["type"] == "number":
            default = Settings.model_fields[key].default
            valid = type(value) in (int, float) and math.isfinite(value)
            valid = valid and (isinstance(default, float) or int(value) == value)
            valid = valid and spec["min"] <= value <= spec["max"]
        else:
            valid = isinstance(value, str) and len(value) <= 4096 and "\x00" not in value
        if not valid:
            raise ValueError(f"{label}的类型或取值范围不正确")
        if key == "llm_provider" and value not in {"", "openai", "anthropic"}:
            raise ValueError("请选择支持的接口协议")
        if key == "llm_model" and not value.strip():
            raise ValueError("模型名称不能为空")
        if key == "llm_api_base":
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("服务地址必须是无凭据、无查询参数的 HTTP(S) 地址")
        if key.endswith("_path") and (not value or not Path(value).is_absolute()):
            raise ValueError(f"{label}必须是绝对路径")
    if values.get("rag_chunk_overlap", 0) >= values.get("rag_chunk_size", 800):
        raise ValueError("相邻片段重叠长度必须小于片段长度")
    return values


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("配置应为对象")
        return validate(raw)
    except (ValueError, OSError) as exc:
        raise ValueError("设置文件无法读取或格式无效，请修复或恢复备份后重试") from exc


def revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()


def view(settings: Settings) -> dict:
    path = settings.preferences_file
    with _LOCK:
        saved = load(path)
        values = {f["key"]: saved.get(f["key"], getattr(settings, f["key"])) for f in fields()}
        values = {k: str(v) if isinstance(v, Path) else v for k, v in values.items()}
        configured = {k: bool(values.pop(k)) for k in SECRETS}
        return dict(
            values=values,
            secrets=configured,
            fields=fields(),
            groups=_GROUPS,
            revision=revision(path),
            desktop=settings.desktop_mode,
            data_dir=str(settings.var_dir),
            config_file=str(path),
            sandbox_available=sys.platform == "linux" and shutil.which("bwrap") is not None,
        )


def candidate(settings: Settings, edits: dict) -> dict:
    values = {f["key"]: getattr(settings, f["key"]) for f in fields()}
    values.update(load(settings.preferences_file))
    values.update(edits)
    return validate({k: str(v) if isinstance(v, Path) else v for k, v in values.items()})


def save(settings: Settings, edits: dict, expected_revision: str) -> str:
    path = settings.preferences_file
    with _LOCK:
        if revision(path) != expected_revision:
            raise FileExistsError("设置已被其他窗口修改，请重新加载后再保存")
        values = candidate(settings, edits)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix=".preferences-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(values, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return revision(path)
