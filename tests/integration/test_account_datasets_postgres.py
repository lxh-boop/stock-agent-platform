"""使用临时 PostgreSQL 的实际隔离验收；永远不得对生产库运行。"""
from __future__ import annotations
import os
from pathlib import Path
from contextlib import contextmanager
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("STOCK_TEST_DATABASE") != "1", reason="requires isolated test database")

@pytest.fixture(scope="module", autouse=True)
def initialize_schema(isolated_postgres_schema):
    from database.auth_schema import ensure_auth_schema
    ensure_auth_schema()
    yield


def test_rankings_do_not_replace_other_user_or_public_rows():
    from core.user_scope import user_scope
    from database.repositories import PredictionRepository
    record = {"date": "2026-11-10", "code": "000001", "model_name": "cross_sectional_ranker", "score": 1, "rank": 1}
    PredictionRepository().replace_snapshot([record], exclusive_scope=True)
    with user_scope("alice"):
        assert PredictionRepository().list_latest_predictions() == []
        PredictionRepository().replace_snapshot([{**record, "score": 2}], exclusive_scope=True)
    with user_scope("bob"):
        assert PredictionRepository().list_latest_predictions() == []
        PredictionRepository().replace_snapshot([{**record, "score": 3}], exclusive_scope=True)
    with user_scope("alice"):
        assert PredictionRepository().list_latest_predictions()[0]["pred_score"] == 2
    with user_scope("bob"):
        assert PredictionRepository().list_latest_predictions()[0]["pred_score"] == 3
    assert PredictionRepository().list_latest_predictions()[0]["pred_score"] == 1


def test_news_same_id_is_still_private():
    from core.user_scope import user_scope
    from database.repositories import NewsRepository
    for user in ("alice", "bob"):
        with user_scope(user):
            NewsRepository().insert_news_event({"news_id": "same_id", "title": user})
    for user in ("alice", "bob"):
        with user_scope(user):
            assert NewsRepository().get_news_event("same_id")["title"] == user
    assert NewsRepository().get_news_event("same_id") is None


def test_account_table_and_existing_portfolio_stay_in_original_schema():
    from core.user_scope import user_scope, user_schema
    from database.connection import get_connection
    from database.repositories.auth_repository import AuthRepository
    from server.api.security import hash_password
    repo = AuthRepository()
    for user in ("alice", "bob"):
        repo.create_account(user_id=user, display_name=user, role="user", password_hash=hash_password("test-only"))
    with get_connection(scoped=False) as conn:
        conn.execute("INSERT INTO paper_account(account_id,user_id,cash) VALUES ('paper_alice','alice',12345)")
        conn.commit()
    with user_scope("alice"):
        repo.save_scheduler_settings("alice", {"auto_retrain_enabled": True})
        assert repo.get_account("alice")["role"] == "user"
        with get_connection() as conn:
            assert conn.execute("SELECT cash FROM paper_account WHERE user_id='alice'").fetchone()["cash"] == 12345
            assert conn.execute("SELECT current_schema() AS name").fetchone()["name"] == user_schema("alice")
    assert repo.scheduler_settings("bob") == {}


def test_account_advisory_lock_blocks_only_same_user():
    from application.user_daily_service import account_lock
    with account_lock("alice", "execute"):
        with pytest.raises(RuntimeError, match="已在运行"):
            with account_lock("alice", "execute"):
                pass
        with account_lock("bob", "execute"):
            pass
    with account_lock("alice", "execute"):
        pass
