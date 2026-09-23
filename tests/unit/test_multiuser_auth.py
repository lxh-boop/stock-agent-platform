from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from server.api import security
from server.api.main import create_app
from server.api.router_factory import _apply_principal_identity
from server.api.security import AuthUser, hash_password, validate_username, verify_password


class FakeAuthRepository:
    """In-memory stand-in for AuthRepository (no PostgreSQL required)."""

    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}

    def count_accounts(self) -> int:
        return len(self.accounts)

    def get_account(self, user_id: str) -> dict[str, Any] | None:
        return self.accounts.get(str(user_id))

    def create_account(
        self, *, user_id: str, password_hash: str, display_name: str, role: str
    ) -> dict[str, Any]:
        record = {
            "user_id": str(user_id),
            "display_name": display_name,
            "role": role,
            "password_hash": password_hash,
            "is_active": True,
            "created_at": "2026-01-01T00:00:00.000+00:00",
            "last_login_at": None,
        }
        self.accounts[str(user_id)] = record
        return dict(record)

    def touch_login(self, user_id: str) -> int:
        if user_id in self.accounts:
            self.accounts[user_id]["last_login_at"] = "2026-01-01T00:00:00.000+00:00"
            return 1
        return 0

    def create_session(self, *, user_id: str, user_agent: str = "") -> dict[str, Any]:
        token = f"token-{len(self.sessions) + 1}-{user_id}"
        record = {
            "session_token": token,
            "user_id": str(user_id),
            "created_at": "2026-01-01T00:00:00.000+00:00",
            "expires_at": "2099-01-01T00:00:00.000+00:00",
            "user_agent": user_agent,
        }
        self.sessions[token] = record
        return dict(record)

    def get_session(self, session_token: str) -> dict[str, Any] | None:
        return self.sessions.get(str(session_token))

    def delete_session(self, session_token: str) -> int:
        return 1 if self.sessions.pop(str(session_token), None) else 0

    def purge_expired_sessions(self) -> int:
        return 0


@pytest.fixture()
def auth_repo() -> FakeAuthRepository:
    repo = FakeAuthRepository()
    previous = security._REPOSITORY
    security.set_auth_repository(repo)
    yield repo
    security.set_auth_repository(previous)


@pytest.fixture()
def client(auth_repo: FakeAuthRepository) -> TestClient:
    return TestClient(create_app())


def test_first_registered_account_becomes_admin(client: TestClient, auth_repo: FakeAuthRepository) -> None:
    first = client.post(
        "/api/v1/auth/register",
        json={"username": "owner", "password": "secret1", "display_name": "管理员"},
    )
    assert first.status_code == 200
    assert first.json()["user"]["role"] == "admin"
    assert first.json()["first_account"] is True
    assert "stock_session" in first.cookies

    second = client.post(
        "/api/v1/auth/register", json={"username": "alice", "password": "secret1"}
    )
    assert second.status_code == 200
    assert second.json()["user"]["role"] == "user"

    owner_token = first.cookies.get("stock_session")
    alice_token = second.cookies.get("stock_session")
    me_owner = client.get("/api/v1/auth/me", cookies={"stock_session": owner_token})
    assert me_owner.status_code == 200
    assert me_owner.json()["user"]["user_id"] == "owner"
    assert me_owner.json()["user"]["role"] == "admin"

    me_alice = client.get("/api/v1/auth/me", cookies={"stock_session": alice_token})
    assert me_alice.status_code == 200
    assert me_alice.json()["user"]["user_id"] == "alice"
    assert me_alice.json()["user"]["role"] == "user"


def test_register_rejects_invalid_username(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register", json={"username": "../etc", "password": "secret1"}
    )
    assert response.status_code == 422


def test_register_rejects_duplicate_username(client: TestClient, auth_repo: FakeAuthRepository) -> None:
    payload = {"username": "dup", "password": "secret1"}
    assert client.post("/api/v1/auth/register", json=payload).status_code == 200
    assert client.post("/api/v1/auth/register", json=payload).status_code == 409


def test_login_logout_flow(client: TestClient, auth_repo: FakeAuthRepository) -> None:
    auth_repo.create_account(
        user_id="bob",
        password_hash=hash_password("password1"),
        display_name="Bob",
        role="user",
    )
    assert (
        client.post("/api/v1/auth/login", json={"username": "bob", "password": "wrong"})
    ).status_code == 401
    assert (
        client.post("/api/v1/auth/login", json={"username": "ghost", "password": "password1"})
    ).status_code == 401

    login = client.post("/api/v1/auth/login", json={"username": "bob", "password": "password1"})
    assert login.status_code == 200
    assert client.get("/api/v1/auth/me").json()["user"]["user_id"] == "bob"

    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_requires_session(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401


def test_require_user_rejects_missing_expired_and_disabled(auth_repo: FakeAuthRepository) -> None:
    def request_with_cookie(token: str) -> Request:
        return Request(
            scope={
                "type": "http",
                "headers": [(b"cookie", f"stock_session={token}".encode())],
            }
        )

    auth_repo.create_account(
        user_id="carol", password_hash="x", display_name="", role="user"
    )
    session = auth_repo.create_session(user_id="carol")

    user = security.require_user(request_with_cookie(session["session_token"]))
    assert user.user_id == "carol"

    with pytest.raises(HTTPException) as missing:
        security.require_user(request_with_cookie(""))
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as unknown:
        security.require_user(request_with_cookie("token-does-not-exist"))
    assert unknown.value.status_code == 401

    auth_repo.sessions[session["session_token"]]["expires_at"] = "2000-01-01T00:00:00.000+00:00"
    with pytest.raises(HTTPException) as expired:
        security.require_user(request_with_cookie(session["session_token"]))
    assert expired.value.status_code == 401


def test_password_hash_roundtrip_and_username_charset() -> None:
    stored = hash_password("s3cret-密码")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret-密码", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret-密码", None)

    assert validate_username("Alice_01") == "Alice_01"
    for bad in ("../etc", "a/b", "a\\b", "", "x" * 33, "-lead"):
        with pytest.raises(ValueError):
            validate_username(bad)


def test_operation_identity_forced_to_principal() -> None:
    plain = AuthUser(user_id="alice", display_name="", role="user")
    admin = AuthUser(user_id="root", display_name="", role="admin")

    forced = _apply_principal_identity({"user_id": "bob", "owner_id": "bob", "top_k": 3}, plain)
    assert forced["user_id"] == "alice"
    assert forced["owner_id"] == "alice"
    assert forced["top_k"] == 3

    untouched = _apply_principal_identity({"user_id": "bob"}, admin)
    assert untouched == {"user_id": "bob"}

    no_identity = _apply_principal_identity({"seconds": 3}, plain)
    assert no_identity == {"seconds": 3}
