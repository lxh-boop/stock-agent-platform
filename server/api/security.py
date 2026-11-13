from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from database.repositories.auth_repository import AuthRepository, utc_now

SESSION_COOKIE_NAME = "stock_session"
SESSION_TTL_SECONDS = 14 * 24 * 3600
_PBKDF2_ITERATIONS = 600_000

# 用户名即目录名/文件名的一部分（agent_sessions/<user_id>、outputs/portfolio/<user_id>），
# 必须在注册时收死字符集，从源头消除路径穿越。
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    display_name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def validate_username(value: str) -> str:
    text = str(value or "").strip()
    if not _USERNAME_PATTERN.fullmatch(text):
        raise ValueError("invalid_username")
    return text


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


_DUMMY_HASH: str | None = None


def verify_password(password: str, stored: str | None) -> bool:
    global _DUMMY_HASH
    if not stored:
        # 账号不存在时也执行一次等价计算，避免通过响应时间探测用户名。
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password("timing-equalizer")
        hash_password(password)
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = str(stored).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


_REPOSITORY: AuthRepository | None = None


def get_auth_repository() -> AuthRepository:
    global _REPOSITORY
    if _REPOSITORY is None:
        _REPOSITORY = AuthRepository()
    return _REPOSITORY


def set_auth_repository(repo: AuthRepository | None) -> None:
    """Swap the repository singleton (used by tests)."""
    global _REPOSITORY
    _REPOSITORY = repo


def _auth_from_account(account: dict[str, Any]) -> AuthUser:
    return AuthUser(
        user_id=str(account.get("user_id") or ""),
        display_name=str(account.get("display_name") or ""),
        role=str(account.get("role") or "user"),
    )


def require_user(request: Request) -> AuthUser:
    principal = getattr(request.state, "authenticated_principal", None)
    if isinstance(principal, AuthUser):
        return principal
    token = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not_authenticated"
        )
    repo = get_auth_repository()
    session = repo.get_session(token)
    if session is None or str(session.get("expires_at") or "") <= utc_now():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="session_invalid_or_expired"
        )
    account = repo.get_account(str(session.get("user_id") or ""))
    if account is None or not bool(account.get("is_active", True)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="account_disabled"
        )
    return _auth_from_account(account)


def require_admin(user: AuthUser = Depends(require_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    return user
