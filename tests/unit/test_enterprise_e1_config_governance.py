from __future__ import annotations

import json
from pathlib import Path


def test_local_config_persists_no_plaintext_secret(monkeypatch, tmp_path: Path) -> None:
    import local_config

    config_path = tmp_path / "local_app_config.json"
    secret_dir = tmp_path / "secret-store"
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STOCK_APP_SECRET_DIR", str(secret_dir))

    local_config.save_local_config(
        {
            "llm_api_key": "llm-secret-value",
            "tushare_token": "tushare-secret-value",
            "neo4j_password": "neo4j-secret-value",
            "llm_mode": "api",
        }
    )

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["llm_api_key"] == ""
    assert persisted["tushare_token"] == ""
    assert persisted["neo4j_password"] == ""
    assert "llm-secret-value" not in config_path.read_text(encoding="utf-8")

    loaded = local_config.load_local_config()
    assert loaded["llm_api_key"] == "llm-secret-value"
    assert loaded["tushare_token"] == "tushare-secret-value"
    assert loaded["neo4j_password"] == "neo4j-secret-value"


def test_legacy_plaintext_secret_migration_is_one_way(monkeypatch, tmp_path: Path) -> None:
    import local_config

    config_path = tmp_path / "local_app_config.json"
    monkeypatch.setattr(local_config, "LOCAL_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STOCK_APP_SECRET_DIR", str(tmp_path / "secrets"))
    config_path.write_text(
        json.dumps(
            {
                "llm_api_key": "legacy-llm",
                "tushare_token": "legacy-ts",
                "neo4j_password": "legacy-neo4j",
                "llm_mode": "api",
            }
        ),
        encoding="utf-8",
    )

    report = local_config.migrate_legacy_local_config_secrets()
    assert report["changed"] is True
    assert set(report["migrated"]) == {"llm_api_key", "tushare_token", "neo4j_password"}

    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted["llm_api_key"] == ""
    assert persisted["tushare_token"] == ""
    assert persisted["neo4j_password"] == ""
    loaded = local_config.load_local_config()
    assert loaded["llm_api_key"] == "legacy-llm"
    assert loaded["tushare_token"] == "legacy-ts"
    assert loaded["neo4j_password"] == "legacy-neo4j"


def test_service_settings_are_typed_and_hashable(monkeypatch) -> None:
    from core.config.service_settings import get_service_settings

    monkeypatch.setenv("STOCK_APP_ENV", "development")
    monkeypatch.setenv("AGENT_API_HOST", "0.0.0.0")
    monkeypatch.setenv("AGENT_API_PORT", "9010")
    monkeypatch.setenv("STOCK_AGENT_MAX_CONCURRENT_TASKS", "7")
    monkeypatch.setenv("AGENT_MAX_PARALLEL_REQUESTS", "2")
    monkeypatch.setenv("AGENT_MAX_PARALLEL_WORKERS", "5")
    monkeypatch.setenv("AGENT_MAX_PARALLEL_TOOLS", "6")
    monkeypatch.setenv("AGENT_MAX_PARALLEL_LLM", "3")

    settings = get_service_settings()
    assert settings.api_port == 9010
    assert settings.max_concurrent_tasks == 7
    assert settings.max_parallel_requests == 2
    assert settings.max_parallel_workers == 5
    assert settings.max_parallel_tools == 6
    assert settings.max_parallel_llm == 3
    assert len(settings.config_hash) == 16
    assert "password" not in json.dumps(settings.public_dict).lower()


def test_production_rejects_wildcard_cors(monkeypatch) -> None:
    from core.config.service_settings import get_service_settings

    monkeypatch.setenv("STOCK_APP_ENV", "production")
    monkeypatch.setenv("STOCK_AGENT_CORS_ORIGINS", "*")
    try:
        get_service_settings()
    except ValueError as exc:
        assert str(exc) == "wildcard_cors_forbidden_in_managed_environment"
    else:
        raise AssertionError("production wildcard CORS was accepted")


def test_secret_file_resolution_never_exposes_value(monkeypatch, tmp_path: Path) -> None:
    from core.config.secrets import resolve_secret

    path = tmp_path / "llm.txt"
    path.write_text("file-managed-secret\n", encoding="utf-8")
    monkeypatch.setenv("STOCK_LLM_API_KEY_FILE", str(path))
    resolved = resolve_secret(
        "llm_api_key",
        file_env_names=("STOCK_LLM_API_KEY_FILE",),
        prefer_external=True,
    )
    assert resolved.value == "file-managed-secret"
    public = resolved.public_dict()
    assert public["configured"] is True
    assert public["source"] == "file_env:STOCK_LLM_API_KEY_FILE"
    assert "file-managed-secret" not in json.dumps(public)


def test_compose_uses_readiness_and_no_default_neo4j_password() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    neo4j = (root / "docker-compose.neo4j.yml").read_text(encoding="utf-8")
    assert "/api/v1/readiness" in compose
    assert "STOCK_APP_SECRET_DIR: /app_secrets" in compose
    assert "stock_daily_app_secrets" in compose
    assert "neo4j/change-me-now" not in neo4j
    assert "NEO4J_PASSWORD is required" in neo4j


def test_api_source_has_distinct_liveness_and_readiness_routes() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "server/api/main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/v1/liveness"' in source
    assert '@app.get("/api/v1/readiness"' in source
    assert "get_startup_report(deep=True)" in source


def test_readiness_distinguishes_core_and_agent_capability(monkeypatch) -> None:
    import core.config.startup as startup

    monkeypatch.setattr(startup, "_check_service_config", lambda: {"ok": True})
    monkeypatch.setattr(startup, "_check_secret_storage", lambda: {"ok": True})
    monkeypatch.setattr(startup, "_check_postgres", lambda *, deep: {"connected": deep})
    monkeypatch.setattr(startup, "_check_llm", lambda: {"configured": True})
    monkeypatch.setattr(startup, "_check_neo4j", lambda *, deep: {"connected": deep})
    monkeypatch.setattr(startup, "_check_tushare", lambda: {"configured": False})

    report = startup.build_startup_report(deep=True)
    assert report.core_ready is True
    assert report.agent_ready is True
    assert report.status == "ready"


def test_readiness_fails_closed_for_core_dependency_without_leaking_error(monkeypatch) -> None:
    import core.config.startup as startup

    monkeypatch.setattr(startup, "_check_service_config", lambda: {"ok": True})
    monkeypatch.setattr(startup, "_check_secret_storage", lambda: {"ok": True})

    def fail_postgres(*, deep: bool):
        raise RuntimeError("password=super-secret-value")

    monkeypatch.setattr(startup, "_check_postgres", fail_postgres)
    monkeypatch.setattr(startup, "_check_llm", lambda: {"configured": True})
    monkeypatch.setattr(startup, "_check_neo4j", lambda *, deep: {"connected": True})
    monkeypatch.setattr(startup, "_check_tushare", lambda: {"configured": True})

    report = startup.build_startup_report(deep=True)
    encoded = json.dumps(report.public_dict(), ensure_ascii=False)
    assert report.core_ready is False
    assert report.status == "not_ready"
    assert "super-secret-value" not in encoded
    assert "password=" not in encoded



def test_readiness_public_details_do_not_expose_infrastructure_identity(monkeypatch) -> None:
    import inspect
    import core.config.startup as startup

    class FakePostgresSettings:
        host = "internal-db.example"
        port = 5432
        database = "private_database"
        app_schema = "private_app_schema"
        runtime_schema = "private_runtime_schema"
        password = "configured-secret"

    monkeypatch.setattr(
        "database.postgres_config.PostgresSettings.from_env",
        lambda: FakePostgresSettings(),
    )

    postgres = startup._check_postgres(deep=False)
    encoded = json.dumps({"postgres": postgres})
    assert "internal-db.example" not in encoded
    assert "private_database" not in encoded
    assert "private_app_schema" not in encoded
    assert "configured-secret" not in encoded

    # The reduced audit package does not include the complete agent.graph package,
    # so verify the Neo4j public-boundary implementation without importing its
    # unrelated graph contracts.
    neo4j_source = inspect.getsource(startup._check_neo4j)
    assert "settings.public_dict()" not in neo4j_source
    assert '"uri"' not in neo4j_source
    assert '"username"' not in neo4j_source
    assert '"database"' not in neo4j_source


def test_api_route_introspection_does_not_initialize_postgres_backed_services() -> None:
    from application.web_agent_service import web_agent_service
    from server.api.main import create_app
    from server.api.tasks import task_manager

    assert task_manager.initialized is False
    assert web_agent_service.initialized is False

    schema = create_app().openapi()
    task_methods = schema["paths"]["/api/v1/tasks"]
    assert "get" in task_methods and "post" in task_methods
    assert "/api/v1/web/agent/sessions" in schema["paths"]

    assert task_manager.initialized is False
    assert web_agent_service.initialized is False

def test_scheduler_public_status_degrades_when_ranking_store_is_unavailable(monkeypatch, tmp_path: Path) -> None:
    import scheduler.runtime_scheduler as runtime_scheduler

    monkeypatch.setattr(
        runtime_scheduler,
        "_scheduler_config",
        lambda: {"enabled": False, "hour": 20, "minute": 0, "catch_up": True},
    )
    monkeypatch.setattr(runtime_scheduler, "load_latest_job_status", lambda root: {})
    monkeypatch.setattr(runtime_scheduler, "_load_runtime_file", lambda root=".": {})
    monkeypatch.setattr(runtime_scheduler, "expected_signal_date", lambda now=None: "2026-08-28")

    def fail_signal_read(*args, **kwargs):
        raise RuntimeError("postgres_credentials_required:password=must-not-leak")

    monkeypatch.setattr(runtime_scheduler, "read_ranking_signal_date", fail_signal_read)

    status = runtime_scheduler.scheduler_public_status(root=tmp_path)
    encoded = json.dumps(status, ensure_ascii=False)

    assert status["ranking_signal_available"] is False
    assert status["ranking_signal_status"] == "unavailable"
    assert status["ranking_signal_error"] == "RuntimeError"
    assert status["latest_signal_date"] == ""
    assert status["market_stale"] is True
    assert "must-not-leak" not in encoded
    assert "password=" not in encoded


def test_scheduler_public_status_distinguishes_empty_from_unavailable(monkeypatch, tmp_path: Path) -> None:
    import scheduler.runtime_scheduler as runtime_scheduler

    monkeypatch.setattr(
        runtime_scheduler,
        "_scheduler_config",
        lambda: {"enabled": False, "hour": 20, "minute": 0, "catch_up": True},
    )
    monkeypatch.setattr(runtime_scheduler, "load_latest_job_status", lambda root: {})
    monkeypatch.setattr(runtime_scheduler, "_load_runtime_file", lambda root=".": {})
    monkeypatch.setattr(runtime_scheduler, "expected_signal_date", lambda now=None: "2026-08-28")
    monkeypatch.setattr(runtime_scheduler, "read_ranking_signal_date", lambda *args, **kwargs: "")

    status = runtime_scheduler.scheduler_public_status(root=tmp_path)

    assert status["ranking_signal_available"] is True
    assert status["ranking_signal_status"] == "empty"
    assert status["ranking_signal_error"] == ""
    assert status["latest_signal_date"] == ""

