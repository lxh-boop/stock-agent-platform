from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from database.auth_schema import ensure_auth_schema
from database.postgres_store import PostgresStore

SESSION_TTL_HOURS = 24 * 14


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)).isoformat(
        timespec="milliseconds"
    )


class AuthRepository:
    """Credential and session storage backing the multi-user auth boundary."""

    def __init__(self) -> None:
        self.store = PostgresStore(scoped=False)

    # -- accounts ---------------------------------------------------------

    def count_accounts(self) -> int:
        ensure_auth_schema()
        row = self.store.fetch_one("SELECT COUNT(*) AS total FROM user_account")
        return int((row or {}).get("total") or 0)

    def get_account(self, user_id: str) -> dict[str, Any] | None:
        ensure_auth_schema()
        return self.store.get("user_account", {"user_id": str(user_id)})

    def create_account(
        self,
        *,
        user_id: str,
        password_hash: str,
        display_name: str,
        role: str,
    ) -> dict[str, Any]:
        ensure_auth_schema()
        return self.store.insert(
            "user_account",
            {
                "user_id": str(user_id),
                "display_name": str(display_name or user_id),
                "role": str(role),
                "password_hash": str(password_hash),
                "is_active": True,
                "created_at": utc_now(),
                "last_login_at": None,
            },
        )

    def account_settings(self, user_id: str) -> dict[str, Any]:
        """Return this account's settings only; never serialize this to a client."""
        account = self.get_account(user_id)
        if not account or not bool(account.get("is_active", True)):
            raise ValueError("account_not_found_or_disabled")
        try:
            config = json.loads(account.get("llm_config_json") or "{}")
        except (ValueError, TypeError):
            config = {}
        return {
            "config": config if isinstance(config, dict) else {},
            "llm_api_key_ciphertext": str(account.get("llm_api_key_ciphertext") or ""),
            "tushare_token_ciphertext": str(account.get("tushare_token_ciphertext") or ""),
        }

    def save_account_settings(self, user_id: str, *, config: dict[str, Any],
                              llm_ciphertext: str, tushare_ciphertext: str) -> None:
        self.account_settings(user_id)
        self.store.update("user_account", {"user_id": str(user_id)}, {
            "llm_config_json": json.dumps(config, ensure_ascii=False),
            "llm_api_key_ciphertext": llm_ciphertext,
            "tushare_token_ciphertext": tushare_ciphertext,
        })

    def scheduler_settings(self, user_id: str) -> dict[str, Any]:
        account = self.get_account(user_id)
        if not account or not account.get("is_active", True):
            raise ValueError("account_not_found_or_disabled")
        value = json.loads(account.get("scheduler_config_json") or "{}")
        return dict(value) if isinstance(value, dict) else {}

    def save_scheduler_settings(self, user_id: str, values: dict[str, Any]) -> None:
        self.scheduler_settings(user_id)
        self.store.update("user_account", {"user_id": str(user_id)}, {
            "scheduler_config_json": json.dumps(values, ensure_ascii=False),
        })

    def list_scheduled_accounts(self) -> list[dict[str, Any]]:
        ensure_auth_schema()
        # 不将密码哈希或 Token 带入调度器遍历结果。
        return self.store.fetch_all(
            "SELECT user_id, scheduler_config_json FROM user_account WHERE is_active=TRUE"
        )

    def touch_login(self, user_id: str) -> int:
        return self.store.update(
            "user_account", {"user_id": str(user_id)}, {"last_login_at": utc_now()}
        )

    # -- sessions ---------------------------------------------------------

    def create_session(self, *, user_id: str, user_agent: str = "") -> dict[str, Any]:
        ensure_auth_schema()
        return self.store.insert(
            "user_session",
            {
                "session_token": uuid.uuid4().hex + uuid.uuid4().hex,
                "user_id": str(user_id),
                "created_at": utc_now(),
                "expires_at": _expiry(),
                "user_agent": str(user_agent or "")[:256],
            },
        )

    def get_session(self, session_token: str) -> dict[str, Any] | None:
        ensure_auth_schema()
        return self.store.get("user_session", {"session_token": str(session_token)})

    def delete_session(self, session_token: str) -> int:
        ensure_auth_schema()
        return self.store.delete("user_session", {"session_token": str(session_token)})

    def purge_expired_sessions(self) -> int:
        ensure_auth_schema()
        return self.store.execute("DELETE FROM user_session WHERE expires_at < %s", (utc_now(),))
