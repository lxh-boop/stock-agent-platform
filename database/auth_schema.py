from __future__ import annotations

import threading

from database.connection import transaction

_LOCK = threading.RLock()
_READY = False

# Credential and session tables live in the application schema next to
# user_profile. DDL is idempotent and applied lazily, mirroring the
# runtime_schema.py pattern used for stock_runtime tables.
_DDL = [
    """CREATE TABLE IF NOT EXISTS user_account (
        user_id VARCHAR(64) PRIMARY KEY,
        display_name VARCHAR(128) NOT NULL DEFAULT '',
        role VARCHAR(16) NOT NULL DEFAULT 'user',
        password_hash TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TEXT NOT NULL,
        last_login_at TEXT
    )""",
    # Existing deployments must gain these columns without rebuilding accounts.
    "ALTER TABLE user_account ADD COLUMN IF NOT EXISTS llm_config_json TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE user_account ADD COLUMN IF NOT EXISTS llm_api_key_ciphertext TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE user_account ADD COLUMN IF NOT EXISTS tushare_token_ciphertext TEXT NOT NULL DEFAULT ''",
    """CREATE TABLE IF NOT EXISTS user_session (
        session_token VARCHAR(64) PRIMARY KEY,
        user_id VARCHAR(64) NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        user_agent TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_session_user ON user_session(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_user_session_expiry ON user_session(expires_at)",
]


def ensure_auth_schema() -> None:
    global _READY
    with _LOCK:
        if _READY:
            return
        with transaction() as conn:
            for ddl in _DDL:
                conn.execute(ddl)
        _READY = True
