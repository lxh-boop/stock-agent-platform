from __future__ import annotations

import json
from pathlib import Path

from application.web_settings_service import WebSettingsApplicationService
from server.api.main import create_app


def test_settings_route_supports_get_and_protected_put() -> None:
    methods = create_app().openapi()["paths"]["/api/v1/web/settings"]
    assert {key for key in methods if key in {"get", "put", "post", "patch", "delete"}} == {"get", "put"}


def test_settings_write_requires_confirmation() -> None:
    service = WebSettingsApplicationService()
    try:
        service.update_settings(
            request_id="r1",
            idempotency_key="i1",
            confirmed=False,
            llm_mode="local",
            api_provider="openai_compatible",
            api_base_url="",
            api_model="gpt-4o-mini",
            api_credential=None,
            clear_api_credential=False,
            local_base_url="http://127.0.0.1:11434/v1",
            local_model="qwen3:4b",
            tushare_credential=None,
            clear_tushare_credential=False,
        )
    except ValueError as exc:
        assert str(exc) == "settings_update_confirmation_required"
    else:
        raise AssertionError("unconfirmed settings write was accepted")


def test_settings_save_preserves_blank_secrets_and_never_returns_values(monkeypatch, tmp_path: Path) -> None:
    import local_config

    config_path = tmp_path / "local_app_config.json"
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STOCK_APP_SECRET_DIR", str(tmp_path / "secrets"))
    local_config.save_local_config({
        "llm_mode": "api",
        "llm_api_provider": "openai_compatible",
        "llm_api_model": "old-model",
        "llm_api_key": "existing-api-secret",
        "tushare_token": "existing-ts-secret",
    })

    service = WebSettingsApplicationService()
    result = service.update_settings(
        request_id="r1",
        idempotency_key="i1",
        confirmed=True,
        llm_mode="api",
        api_provider="deepseek",
        api_base_url="https://api.deepseek.com/v1",
        api_model="deepseek-chat",
        api_credential=None,
        clear_api_credential=False,
        local_base_url="http://127.0.0.1:11434/v1",
        local_model="qwen3:4b",
        tushare_credential=None,
        clear_tushare_credential=False,
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    # Enterprise E1: credentials are no longer persisted in local_app_config.json.
    assert saved["llm_api_key"] == ""
    assert saved["tushare_token"] == ""
    loaded = local_config.load_local_config()
    assert loaded["llm_api_key"] == "existing-api-secret"
    assert loaded["tushare_token"] == "existing-ts-secret"
    encoded = json.dumps(result, ensure_ascii=False)
    assert "existing-api-secret" not in encoded
    assert "existing-ts-secret" not in encoded
    assert result["settings"]["configuration"]["api"]["custom_configured"] is True


def test_local_profile_accepts_docker_host_bridge(monkeypatch) -> None:
    from core.llm.profiles import build_model_profile

    monkeypatch.setenv("STOCK_LOCAL_LLM_BASE_URL", "http://host.docker.internal:11434/v1")
    profile = build_model_profile(
        {"llm_local_base_url": "http://127.0.0.1:11434/v1", "llm_local_model": "qwen3:4b"},
        mode="local",
        credential_ref="none",
    )
    assert profile.endpoint_scope == "loopback"
    assert "host.docker.internal" in profile.base_url


def test_tushare_loader_prefers_saved_local_credential(monkeypatch, tmp_path: Path) -> None:
    import local_config
    from data_tushare import get_token

    config_path = tmp_path / "local_app_config.json"
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STOCK_APP_SECRET_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("TUSHARE_TOKEN", "environment-token")
    local_config.save_local_config({"tushare_token": "saved-token"})
    assert get_token() == "saved-token"
