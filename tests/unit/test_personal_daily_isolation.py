from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.user_scope import current_owner, owned_path, user_scope, user_schema, workspace, worker_environment
from scheduler.account_scheduler import normalize_schedule, due_slot, completed_signal_date
from core.config.user_credentials import encrypt_credential
import application.user_credentials_service as credentials


@pytest.fixture
def private_root(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_USER_WORKSPACE_ROOT", str(tmp_path / "users"))
    monkeypatch.delenv("STOCK_DATA_OWNER_ID", raising=False)
    monkeypatch.setenv("STOCK_USER_CREDENTIAL_KEY", Fernet.generate_key().decode())
    return tmp_path / "users"


class Accounts:
    rows = {}
    schedules = {}
    def account_settings(self, user):
        return self.rows[user]
    def save_account_settings(self, user, *, config, llm_ciphertext, tushare_ciphertext):
        self.rows[user] = {"config": dict(config), "llm_api_key_ciphertext": llm_ciphertext,
                           "tushare_token_ciphertext": tushare_ciphertext}
    def scheduler_settings(self, user):
        return self.schedules.get(user, {})
    def save_scheduler_settings(self, user, value):
        self.schedules[user] = dict(value)


@pytest.fixture
def accounts(private_root, monkeypatch):
    Accounts.rows = {u: {"config": {"llm_mode": "api", "llm_api_model": "test-model"},
                        "llm_api_key_ciphertext": encrypt_credential(u + "-llm"),
                        "tushare_token_ciphertext": encrypt_credential(u + "-tushare")}
                     for u in ("alice", "bob")}
    Accounts.schedules = {}
    monkeypatch.setattr(credentials, "AuthRepository", Accounts)
    return Accounts


def test_scopes_restore_and_paths_are_not_shared(private_root):
    assert not current_owner()
    with user_scope("alice"):
        a = owned_path("data/latest_raw_stock_data.csv")
        assert a == private_root / "alice/data/latest_raw_stock_data.csv"
        assert owned_path(a) == a
        assert owned_path("data\\csi300_stock_pool.csv") == private_root / "alice/data/csi300_stock_pool.csv"
        with user_scope("bob"):
            assert owned_path("outputs/ranking_latest.csv") == private_root / "bob/outputs/ranking_latest.csv"
            with pytest.raises(ValueError, match="cross_user"):
                owned_path(a)
        assert current_owner() == "alice"
    assert not current_owner()
    assert user_schema("alice") != user_schema("Alice")
    with pytest.raises(ValueError):
        workspace("../alice")


def test_parallel_request_contexts_do_not_mutate_environment(private_root):
    before = dict(os.environ)
    cwd = Path.cwd()
    async def read(user):
        with user_scope(user):
            await asyncio.sleep(0)
            return current_owner(), owned_path("outputs/test.csv")
    async def run():
        return await asyncio.gather(read("alice"), read("bob"))
    values = asyncio.run(run())
    assert values == [("alice", private_root / "alice/outputs/test.csv"),
                      ("bob", private_root / "bob/outputs/test.csv")]
    assert dict(os.environ) == before
    assert Path.cwd() == cwd


def test_worker_import_uses_private_data_but_shared_model_assets(private_root):
    root = workspace("alice")
    root.mkdir(parents=True)
    env = worker_environment("alice")
    assert all(k not in env for k in ("TUSHARE_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY"))
    script = "import config,json;print(json.dumps([config.DATA_DIR,config.OUTPUT_DIR,config.ACTIVE_RANKING_MODEL_MANIFEST_PATH,config.MODEL_DIR,config.ACTIVE_RANKING_MODEL_METRICS_PATH]))"
    data, output, manifest, models, metrics = json.loads(subprocess.check_output(
        [sys.executable, "-c", script], cwd=root, env=env, text=True))
    assert Path(data) == root / "data"
    assert Path(output) == root / "outputs"
    assert Path(manifest).is_file()
    assert Path(models).is_absolute() and not Path(models).is_relative_to(root)
    assert Path(metrics).is_relative_to(root / "outputs")


def test_personal_token_wins_and_missing_never_falls_back(accounts, monkeypatch):
    from data_tushare import get_token
    from core.config.secrets import resolve_secret
    monkeypatch.setenv("TUSHARE_TOKEN", "platform-token")
    monkeypatch.setenv("LLM_API_KEY", "platform-key")
    for user in ("alice", "bob"):
        with user_scope(user):
            assert get_token() == user + "-tushare"
            assert resolve_secret("llm_api_key", env_names=("LLM_API_KEY",)).value == user + "-llm"
    accounts.rows["bob"]["tushare_token_ciphertext"] = ""
    accounts.rows["bob"]["llm_api_key_ciphertext"] = ""
    with user_scope("bob"):
        with pytest.raises(ValueError, match="user_tushare_token_not_configured"):
            get_token()
        assert resolve_secret("llm_api_key", env_names=("LLM_API_KEY",)).value == ""


def test_normal_user_saves_only_own_schedule(accounts, monkeypatch):
    monkeypatch.setattr(credentials, "public_settings_for_user", lambda user: {"current_user_id": user})
    import local_config
    monkeypatch.setattr(local_config, "save_local_config", lambda _: pytest.fail("global config write"))
    values = {"confirmed": True, "llm_mode": "api", "api_model": "test-model",
              "scheduler_enabled": True, "scheduler_hour": 0, "scheduler_minute": 30,
              "scheduler_catch_up": False, "request_id": "r", "idempotency_key": "i"}
    credentials.save_settings_for_user("alice", is_admin=False, values=values)
    assert accounts.schedules["alice"]["auto_retrain_hour"] == 0
    assert "bob" not in accounts.schedules
    assert credentials.runtime_config_for_user("alice")["auto_retrain_enabled"] is True
    assert credentials.runtime_config_for_user("bob")["auto_retrain_enabled"] is False


def test_schedule_slot_stable_and_disabled_makes_no_task():
    now = datetime(2026, 11, 10, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    cfg = {"auto_retrain_enabled": True, "auto_retrain_hour": 20}
    assert due_slot(cfg, now) == "2026-11-10T20:00+08:00"
    assert due_slot(cfg, now.replace(minute=30)) == due_slot(cfg, now)
    assert due_slot({}, now) == ""
    assert due_slot({**cfg, "auto_retrain_catch_up": False}, now) == ""
    assert completed_signal_date(now.replace(hour=10)) == "2026-11-09"
    assert completed_signal_date(now.replace(hour=20)) == "2026-11-10"
    with pytest.raises(ValueError):
        normalize_schedule({"auto_retrain_hour": 24})


def test_ordinary_user_can_submit_daily_but_cannot_choose_other_owner(monkeypatch):
    from server.api.tasks import router
    from server.api.security import require_user, AuthUser
    import application.user_daily_service as daily
    captured = []
    def submit(user, **kwargs):
        captured.append((user, kwargs))
        return {"task_id": "task_test", "owner_id": user, "status": "queued"}
    monkeypatch.setattr(daily, "submit_personal_daily", submit)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthUser("alice", "", "user")
    with TestClient(app) as client:
        result = client.post("/api/v1/tasks", json={
            "task_type": "paper-profile.scheduler-manual", "owner_id": "bob",
            "kwargs": {"user_id": "bob", "all_users": True, "output_dir": "/tmp/other", "token": "evil"},
        })
        assert result.status_code == 200
        assert result.json()["success"] is True
        assert captured == [("alice", {"session_id": ""})]
        assert client.post("/api/v1/tasks", json={"task_type": "paper-profile.scheduler-manual", "args": ["bob"]}).status_code == 422


def test_task_submission_is_owner_only_idempotent_and_has_no_token(accounts, monkeypatch):
    import application.user_daily_service as daily
    from server.api import tasks
    from contextlib import nullcontext
    records, active = [], []
    class Store:
        def list(self, **kwargs):
            assert kwargs["owner_id"] == "alice"
            return active if kwargs.get("active_only") else records
    def submit(**kwargs):
        assert kwargs["owner_id"] == "alice"
        assert kwargs["kwargs"]["all_users"] is False
        assert "alice-tushare" not in json.dumps(kwargs)
        row = {"task_id": "task_a", **kwargs}
        records.append(row)
        return row
    monkeypatch.setattr(tasks, "task_manager", SimpleNamespace(store=Store(), submit=submit))
    monkeypatch.setattr(daily, "account_lock", lambda *args: nullcontext())
    first = daily.submit_personal_daily("alice", source="scheduled", slot="2026-11-10T20:00")
    assert daily.submit_personal_daily("alice", source="scheduled", slot="2026-11-10T20:00") == first
    assert len(records) == 1
    active.append(first)
    assert daily.submit_personal_daily("alice") == first
    assert len(records) == 1


def test_asgi_scope_reaches_sync_route(private_root, monkeypatch):
    from server.api.user_scope_middleware import UserScopeMiddleware
    from server.api import security
    monkeypatch.setattr(security, "require_user", lambda request: security.AuthUser(request.headers["x-test-user"], "", "user"))
    app = FastAPI()
    app.add_middleware(UserScopeMiddleware)
    @app.get("/api/v1/scope-test")
    def read():
        return {"user": current_owner(), "path": str(owned_path("data/test.csv"))}
    with TestClient(app) as client:
        for user in ("alice", "bob"):
            result = client.get("/api/v1/scope-test", headers={"x-test-user": user}).json()
            assert result == {"user": user, "path": str(private_root / user / "data/test.csv")}
    assert not current_owner()
