from __future__ import annotations

import importlib
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pandas as pd
import pytest

from core.user_scope import current_owner, user_scope


@pytest.fixture
def monitor_db(monkeypatch, tmp_path):
    from evaluation import system_monitor as monitor
    from database.postgres_store import PostgresStore

    connections = {}
    queries = []
    for owner, news_count in (("alice", 3), ("bob", 1)):
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE news_event(content_level TEXT);
            CREATE TABLE agent_runs(run_id TEXT, user_id TEXT, status TEXT, created_at TEXT,
                started_at TEXT, finished_at TEXT, metadata_json TEXT);
            CREATE TABLE agent_tool_calls(user_id TEXT, status TEXT, started_at TEXT,
                error_type TEXT, error_message TEXT, retry_count INTEGER);
            CREATE TABLE agent_steps(run_id TEXT, started_at TEXT);
            CREATE TABLE agent_sources(run_id TEXT, user_id TEXT, retrieved_at TEXT);
            CREATE TABLE portfolio_position(user_id TEXT, stock_code TEXT, market_value REAL,
                industry TEXT, updated_at TEXT);
            CREATE TABLE system_monitor_snapshots(snapshot_id TEXT, user_id TEXT,
                updated_at TEXT, trade_date TEXT, overall_status TEXT, data_metrics_json TEXT);
            CREATE TABLE system_monitor_alerts(alert_id TEXT, snapshot_id TEXT, user_id TEXT,
                updated_at TEXT, message TEXT);
        """)
        db.executemany("INSERT INTO news_event VALUES (?)", [("full_text",)] * news_count)
        db.executemany("INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?)", [
            ("alice-run", "alice", "completed", "2026-11-18", "2026-11-18 01:00:00", "2026-11-18 01:00:10", "{}"),
            ("bob-run", "bob", "failed", "2026-11-18", "2026-11-18 01:00:00", "2026-11-18 01:01:00", "{}"),
        ])
        db.executemany("INSERT INTO agent_tool_calls VALUES (?,?,?,?,?,?)", [
            ("alice", "succeeded", "2026-11-18", "", "", 0),
            ("bob", "failed", "2026-11-18", "TimeoutError", "synthetic-secret", 2),
        ])
        db.executemany("INSERT INTO agent_steps VALUES (?,?)", [("alice-run", "2026-11-18"), ("bob-run", "2026-11-18")])
        db.executemany("INSERT INTO agent_sources VALUES (?,?,?)", [("alice-run", "alice", "2026-11-18"), ("bob-run", "bob", "2026-11-18")])
        db.executemany("INSERT INTO portfolio_position VALUES (?,?,?,?,?)", [
            ("alice", "000001", 100.0, "bank", "2026-11-18"),
            ("bob", "000002", 900.0, "estate", "2026-11-18"),
        ])
        for account in ("alice", "bob"):
            db.execute("INSERT INTO system_monitor_snapshots VALUES (?,?,?,?,?,?)", (account, account, "2026-11-18", "2026-11-18", "normal", json.dumps({"owner": account})))
            db.execute("INSERT INTO system_monitor_alerts VALUES (?,?,?,?,?)", (account, account, account, "2026-11-18", account + " alert"))
        connections[owner] = db

    class ReadOnlyConnection:
        def __init__(self):
            self.db = connections[current_owner() or "alice"]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def close(self):
            pass

        def execute(self, sql, params=()):
            assert sql.lstrip().upper().startswith("SELECT"), "Monitoring must not write"
            queries.append((current_owner(), sql, tuple(params)))
            if "to_regclass" in sql:
                return self.db.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name=?) AS present", params)
            return self.db.execute(sql.replace("%s", "?"), params)

    monkeypatch.setattr(monitor, "get_connection", ReadOnlyConnection)
    monkeypatch.setattr(PostgresStore, "__init__", lambda self: None)
    monkeypatch.setattr(PostgresStore, "_connect", lambda self: ReadOnlyConnection())
    monkeypatch.setattr(monitor, "latest_alpha158", lambda: pd.DataFrame())
    monkeypatch.setenv("STOCK_USER_WORKSPACE_ROOT", str(tmp_path / "users"))
    monkeypatch.delenv("STOCK_DATA_OWNER_ID", raising=False)
    # Shared files must never supply another account's metrics.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "ranking_latest.csv").write_text("code,pred_score\n999999,99\n")
    for owner, score in (("alice", 0.4), ("bob", 0.9)):
        path = tmp_path / "users" / owner / "outputs"
        path.mkdir(parents=True)
        (path / "ranking_latest.csv").write_text(f"code,pred_score\n000001,{score}\n")
    yield monitor, queries
    for db in connections.values():
        db.close()


@pytest.fixture
def client(monitor_db):
    from server.api.routers.web_monitor import router
    from server.api.security import AuthUser, require_user

    account = {"id": "alice"}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthUser(account["id"], account["id"], "user")
    with TestClient(app) as browser:
        yield browser, account


def test_monitor_application_imports_without_removed_agent_runtime():
    service = importlib.import_module("application.system_monitor_service")
    assert callable(service.build_system_monitor_snapshot)
    assert callable(service.build_agent_runtime_health_summary)


def test_all_monitor_routes_use_real_services_and_account_data(client):
    browser, account = client
    for owner, expected_news, expected_score in (("alice", 3, 0.4), ("bob", 1, 0.9)):
        account["id"] = owner
        payload = browser.get("/api/v1/web/monitor/summary?user_id=someone_else").json()
        assert payload["success"] is True
        snapshot = payload["data"]["snapshot"]
        assert snapshot["user_id"] == owner
        assert snapshot["data_metrics"]["news_count"] == expected_news
        assert snapshot["model_metrics"]["prediction_mean"] == expected_score
        assert snapshot["agent_metrics"]["agent_run_count"] == 1
        assert snapshot["run_id"] == owner + "-run"
        assert "synthetic-secret" not in json.dumps(payload)
        assert "thresholds_path" not in snapshot["version_info"]
        service = browser.get("/api/v1/web/monitor/services").json()
        assert service["success"] is True
        health = service["data"]["agent_runtime"]
        assert health["run_count"] == 1
        assert health["status"] == ("ok" if owner == "alice" else "warning")
        assert health["retry_count"] == (0 if owner == "alice" else 2)
        for endpoint in ("history", "alerts"):
            records = browser.get("/api/v1/web/monitor/" + endpoint).json()
            assert records["success"] is True
            assert [row["user_id"] for row in records["data"]["records"]] == [owner]
    assert current_owner() == ""


def test_active_owner_overrides_callers_and_portfolio_export_cannot_bypass_filter(monitor_db, tmp_path):
    monitor, queries = monitor_db
    export = tmp_path / "users" / "alice" / "outputs" / "portfolio" / "alice"
    export.mkdir(parents=True)
    (export / "paper_positions_latest.csv").write_text("user_id,stock_code\nbob,000002\n")
    with user_scope("alice"):
        result = monitor.build_system_monitor_snapshot(user_id="bob")
        assert result.snapshot["user_id"] == "alice"
        assert result.snapshot["run_id"] == "alice-run"
        assert result.snapshot["agent_metrics"]["average_steps"] == 1
        positions, source = monitor._load_current_position_rows(None, "alice", "outputs")
        assert [row["stock_code"] for row in positions] == ["000001"]
        assert source == "portfolio_position_table"
        assert monitor.collect_runtime_health_summary(user_id="bob")["success_rate"] == 1
        assert monitor.list_system_monitor_history(user_id="bob")[0]["user_id"] == "alice"
    assert any("to_regclass" in sql for _, sql, _ in queries)
    assert all(params == ("alice",) for _, sql, params in queries if "FROM agent_" in sql and "to_regclass" not in sql)


def test_missing_records_do_not_claim_runtime_is_healthy(monitor_db, monkeypatch):
    monitor, _ = monitor_db
    monkeypatch.setattr(monitor, "_table_exists", lambda *_: False)
    result = monitor.collect_runtime_health_summary(user_id="alice")
    assert result["status"] == "no_data"
    assert result["run_count"] == 0
    assert result["success_rate"] is None


def test_database_errors_are_visible_without_connection_secrets(client, monkeypatch):
    from evaluation import system_monitor as monitor

    def unavailable():
        raise RuntimeError("postgresql://synthetic-user:synthetic-password@host/database")

    monkeypatch.setattr(monitor, "get_connection", unavailable)
    browser, _ = client
    summary = browser.get("/api/v1/web/monitor/summary")
    assert summary.json()["success"] is False
    assert summary.json()["error"]["details"]["error_type"] == "RuntimeError"
    service = browser.get("/api/v1/web/monitor/services")
    assert service.json()["data"]["agent_runtime"] == {"status": "unavailable", "error": "RuntimeError"}
    assert "synthetic-password" not in summary.text + service.text


def test_monitor_routes_require_authentication():
    from server.api.routers.web_monitor import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as browser:
        for endpoint in ("summary", "services", "history", "alerts"):
            assert browser.get("/api/v1/web/monitor/" + endpoint).status_code == 401
