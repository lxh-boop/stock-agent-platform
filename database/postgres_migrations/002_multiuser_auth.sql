-- 002_multiuser_auth: 账号密码登录与 Cookie 会话（多用户支持）
-- 与 database/auth_schema.py 保持一致；runtime 由 ensure_auth_schema() 懒加载保证。
CREATE TABLE IF NOT EXISTS user_account (
    user_id VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL DEFAULT '',
    role VARCHAR(16) NOT NULL DEFAULT 'user',
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS user_session (
    session_token VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_user_session_user ON user_session(user_id);
CREATE INDEX IF NOT EXISTS idx_user_session_expiry ON user_session(expires_at);
