from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from server.api.security import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    AuthUser,
    get_auth_repository,
    hash_password,
    require_user,
    validate_username,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=64)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


def _public_user(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(account.get("user_id") or ""),
        "display_name": str(account.get("display_name") or ""),
        "role": str(account.get("role") or "user"),
    }


def _issue_session(response: Response, account: dict[str, Any], user_agent: str = "") -> None:
    repo = get_auth_repository()
    repo.purge_expired_sessions()
    session = repo.create_session(user_id=str(account["user_id"]), user_agent=user_agent)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        str(session["session_token"]),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


@router.post("/register")
def register(payload: RegisterRequest, response: Response, request: Request) -> dict[str, Any]:
    try:
        username = validate_username(payload.username)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_username")
    repo = get_auth_repository()
    if repo.get_account(username) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "username_taken")
    first_account = repo.count_accounts() == 0
    role = "admin" if first_account else "user"
    try:
        account = repo.create_account(
            user_id=username,
            password_hash=hash_password(payload.password),
            display_name=(payload.display_name or username)[:64],
            role=role,
        )
    except Exception:
        if repo.get_account(username) is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "username_taken")
        raise
    _issue_session(response, account, request.headers.get("user-agent", ""))
    return {"user": _public_user(account), "first_account": first_account}


@router.post("/login")
def login(payload: LoginRequest, response: Response, request: Request) -> dict[str, Any]:
    repo = get_auth_repository()
    account = repo.get_account(payload.username.strip())
    stored_hash = str(account.get("password_hash") or "") if account else None
    if not verify_password(payload.password, stored_hash or None):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")
    if account is not None and not bool(account.get("is_active", True)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "account_disabled")
    repo.touch_login(str(account["user_id"]))
    _issue_session(response, account, request.headers.get("user-agent", ""))
    return {"user": _public_user(account)}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE_NAME) or ""
    if token:
        get_auth_repository().delete_session(token)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: AuthUser = Depends(require_user)) -> dict[str, Any]:
    return {
        "user": {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "role": user.role,
        }
    }
