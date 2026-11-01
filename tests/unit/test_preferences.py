"""Desktop settings never depend on .env or expose stored credentials."""

import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dbchat import preferences
from dbchat.app import create_app
from dbchat.desktop import desktop_settings
from dbchat.di import build_context
from dbchat.storage import Storage


def test_desktop_ignores_environment_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "environment-secret")
    monkeypatch.setenv("DB_PASSWORD", "database-secret")
    settings = desktop_settings(tmp_path, tmp_path, "a" * 64)
    assert not settings.llm_api_key
    assert not settings.db_password
    assert not settings.db_name
    assert settings.desktop_mode
    assert settings.host == "127.0.0.1" and settings.port == 0


def test_private_atomic_preferences_and_secret_preservation(tmp_path):
    settings = desktop_settings(tmp_path, tmp_path, "a" * 64)
    initial = preferences.view(settings)
    version = preferences.save(
        settings, {"llm_api_key": "synthetic-secret", "llm_model": "custom"}, initial["revision"]
    )
    assert stat.S_IMODE(settings.preferences_file.stat().st_mode) == 0o600
    visible = preferences.view(settings)
    assert "synthetic-secret" not in str(visible)
    assert visible["secrets"]["llm_api_key"]
    preferences.save(settings, {"query_row_limit": 42}, version)
    restarted = desktop_settings(tmp_path, tmp_path, "b" * 64)
    assert restarted.query_row_limit == 42
    assert restarted.llm_api_key == "synthetic-secret"
    with pytest.raises(FileExistsError):
        preferences.save(settings, {"llm_api_key": "other"}, version)
    preferences.save(settings, {"llm_api_key": ""}, preferences.revision(settings.preferences_file))
    assert not preferences.view(settings)["secrets"]["llm_api_key"]


@pytest.mark.parametrize(
    "values",
    [
        {"host": "0.0.0.0"},
        {"query_row_limit": 10001},
        {"query_row_limit": True},
        {"llm_temperature": float("nan")},
        {"rag_chunk_size": 100, "rag_chunk_overlap": 100},
        {"llm_api_base": "file:///tmp/key"},
        {"llm_api_base": "https://secret:pass@example.com"},
        {"llm_model": ""},
        {"rag_doc_path": "relative.md"},
        {"llm_provider": "unsupported"},
    ],
)
def test_invalid_preferences_are_rejected(tmp_path, values):
    settings = desktop_settings(tmp_path, tmp_path, "a" * 64)
    with pytest.raises(ValueError):
        preferences.save(settings, values, preferences.revision(settings.preferences_file))
    assert not settings.preferences_file.exists()


async def test_settings_api_permissions_redaction_and_model_test(tmp_path, monkeypatch):
    settings = desktop_settings(tmp_path, tmp_path, "a" * 64)
    storage = Storage(settings)
    storage.migrate()
    ctx = build_context(
        settings, storage=storage, connections=SimpleNamespace(close_all=AsyncMock())
    )
    ctx.settings.auth_user_token = "user-token"
    app = create_app(settings, context=ctx, build_rag=False)
    complete = AsyncMock(return_value="OK")
    monkeypatch.setattr("dbchat.api.routes.preferences.LLMClient.complete", complete)
    try:
        assert await ctx.list_profiles() == []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/admin/settings")).status_code == 401
            assert (
                await client.get("/api/admin/settings", auth=("user", "user-token"))
            ).status_code == 403
            client.auth = ("admin", "a" * 64)
            initial = (await client.get("/api/admin/settings")).json()
            payload = {"revision": initial["revision"], "values": {"llm_api_key": "test-key"}}
            response = await client.put("/api/admin/settings", json=payload)
            assert response.status_code == 200
            assert "test-key" not in response.text
            assert (await client.put("/api/admin/settings", json=payload)).status_code == 409
            response = await client.post("/api/admin/settings/test-model", json={"values": {}})
            assert response.status_code == 200, response.text
            complete.assert_awaited_once()
            complete.side_effect = RuntimeError("test-key: unsafe provider details")
            response = await client.post("/api/admin/settings/test-model", json={"values": {}})
            assert response.status_code == 502
            assert "test-key" not in response.text
    finally:
        storage.close()
