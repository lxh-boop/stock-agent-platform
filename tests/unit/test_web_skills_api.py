from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.api.routers.web_skills as web_skills
from agent.skill_runtime import SkillRegistry
from server.api import security
from server.api.main import create_app
from tests.unit.test_multiuser_auth import FakeAuthRepository, hash_password


VALID_MD = """---
name: packed_skill
version: 1.0.0
description: 打包导入的技能。
---

## Purpose

打包正文。
"""


@pytest.fixture()
def auth_repo() -> FakeAuthRepository:
    repo = FakeAuthRepository()
    previous = security._REPOSITORY
    security.set_auth_repository(repo)
    repo.create_account(user_id="alice", password_hash=hash_password("secret1"), display_name="A", role="user")
    repo.create_account(user_id="bob", password_hash=hash_password("secret1"), display_name="B", role="user")
    yield repo
    security.set_auth_repository(previous)


@pytest.fixture()
def skill_registry(tmp_path: Path) -> SkillRegistry:
    builtin_root = tmp_path / "builtin"
    skill_dir = builtin_root / "platform_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: platform_skill\ndescription: 内置合规检查。\n---\n\n正文",
        encoding="utf-8",
    )
    registry = SkillRegistry(builtin_dir=builtin_root, user_root=tmp_path / "runtime" / "skills")
    previous = web_skills._registry
    web_skills._registry = lambda: registry
    yield registry
    web_skills._registry = previous


@pytest.fixture()
def client(auth_repo: FakeAuthRepository) -> TestClient:
    return TestClient(create_app())


def _alice(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": "alice", "password": "secret1"})
    assert response.status_code == 200
    return {"stock_session": response.cookies.get("stock_session")}


def _bob(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"username": "bob", "password": "secret1"})
    assert response.status_code == 200
    return {"stock_session": response.cookies.get("stock_session")}


def test_skills_require_login(client: TestClient, skill_registry: SkillRegistry) -> None:
    assert client.get("/api/v1/web/skills").status_code in (401, 403)


def test_list_contains_builtin_but_not_other_users(
    client: TestClient, auth_repo: FakeAuthRepository, skill_registry: SkillRegistry
) -> None:
    skill_registry.create_from_editor("bob", name="bob_skill", description="d", body="正文")
    cookies = _alice(client)

    payload = client.get("/api/v1/web/skills", cookies=cookies).json()["data"]
    names = {skill["name"] for skill in payload["skills"]}
    # Alice 只能看到内置技能；Bob 的个人技能对她不可见。
    assert names == {"platform_skill"}
    assert payload["user_id"] == "alice"


def test_create_enable_disable_delete_flow(
    client: TestClient, auth_repo: FakeAuthRepository, skill_registry: SkillRegistry
) -> None:
    cookies = _alice(client)

    created = client.post(
        "/api/v1/web/skills",
        json={"name": "my_skill", "description": "我的技能", "body": "## Purpose\n检查。"},
        cookies=cookies,
    )
    assert created.status_code == 200
    assert created.json()["data"]["skill"]["status"] == "admitted"

    detail = client.get("/api/v1/web/skills/my_skill", cookies=cookies)
    assert detail.status_code == 200
    assert "检查。" in detail.json()["data"]["skill"]["body"]

    disabled = client.post("/api/v1/web/skills/my_skill/disable", cookies=cookies)
    assert disabled.json()["data"]["skill"]["status"] == "disabled"
    enabled = client.post("/api/v1/web/skills/my_skill/enable", cookies=cookies)
    assert enabled.json()["data"]["skill"]["status"] == "admitted"

    assert client.delete("/api/v1/web/skills/my_skill", cookies=cookies).status_code == 200
    missing = client.get("/api/v1/web/skills/my_skill", cookies=cookies)
    assert missing.json()["success"] is False


def test_builtin_skill_is_read_only(
    client: TestClient, auth_repo: FakeAuthRepository, skill_registry: SkillRegistry
) -> None:
    cookies = _alice(client)
    response = client.delete("/api/v1/web/skills/platform_skill", cookies=cookies)
    assert response.json()["success"] is False
    assert response.json()["error"]["details"]["reason"] == "builtin_skill_immutable"


def test_upload_zip_creates_skill(
    client: TestClient, auth_repo: FakeAuthRepository, skill_registry: SkillRegistry, tmp_path: Path
) -> None:
    package = tmp_path / "skill.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("repo-main/SKILL.md", VALID_MD)
    cookies = _alice(client)

    response = client.post(
        "/api/v1/web/skills/upload", files={"file": ("skill.zip", package.read_bytes())}, cookies=cookies
    )
    assert response.status_code == 200
    assert response.json()["data"]["skill"]["name"] == "packed_skill"


def test_import_github_rejects_non_github_url(
    client: TestClient, auth_repo: FakeAuthRepository, skill_registry: SkillRegistry
) -> None:
    cookies = _alice(client)
    response = client.post(
        "/api/v1/web/skills/import-github",
        json={"url": "https://gitlab.com/owner/repo"},
        cookies=cookies,
    )
    assert response.json()["success"] is False
    assert response.json()["error"]["details"]["reason"] == "unsupported_github_url"


def test_invalid_skill_name_rejected_with_safe_reason(
    client: TestClient, auth_repo: FakeAuthRepository, skill_registry: SkillRegistry
) -> None:
    cookies = _alice(client)
    response = client.post(
        "/api/v1/web/skills",
        json={"name": "../evil", "description": "d", "body": "正文"},
        cookies=cookies,
    )
    assert response.json()["success"] is False
    assert response.json()["error"]["details"]["reason"] == "invalid_skill_name"
