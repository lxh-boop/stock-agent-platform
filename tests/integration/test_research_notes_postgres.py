"""Real SQL against a local temporary database only; no production credentials."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from psycopg import sql

from application.research_notes_service import ResearchNotesService
from core.user_scope import user_scope, user_schema
from database.connection import get_connection

pytestmark = pytest.mark.skipif(os.environ.get("STOCK_TEST_DATABASE") != "1", reason="temporary DB only")


@pytest.fixture
def owners(isolated_postgres_schema):
    users = ("notes_a_" + uuid4().hex[:10], "notes_b_" + uuid4().hex[:10])
    yield users
    with get_connection(scoped=False) as conn:
        for owner in users:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(user_schema(owner))))
        conn.commit()


def add(slug="prefer-momentum", **changes):
    return {"op": "add", "slug": slug, "title": "研究偏好", "content": "优先比较动量与价值因子。",
            "request_id": str(uuid4()), "explicit_user_request": True, **changes}


def test_note_roundtrip_replay_revision_and_recreated_slug(owners):
    owner, _ = owners
    service = ResearchNotesService(owner)
    with user_scope(owner):
        first = add()
        receipt = service.save(**first)
        assert service.save(**first) == receipt
        assert service.read()["total"] == 1
        with pytest.raises(ValueError, match="request_id_conflict"):
            service.save(**{**first, "content": "另一个内容"})
        with pytest.raises(ValueError, match="already_exists"):
            service.save(**add())
        update = add(op="update", expected_revision=receipt["revision"], content="加入换手率约束。")
        updated = service.save(**update)
        assert updated["revision"] > receipt["revision"]
        assert service.save(**update) == updated
        assert service.read(slug=first["slug"])["records"][0]["content"] == "加入换手率约束。"
        deletion = add(op="delete", title="", content="", expected_revision=updated["revision"])
        removed = service.save(**deletion)
        assert service.save(**deletion) == removed and removed["deleted"]
        assert service.read()["total"] == 0
        recreated = service.save(**add())
        assert recreated["revision"] > updated["revision"]
        with pytest.raises(ValueError, match="revision_conflict"):
            service.save(**{**deletion, "request_id": str(uuid4())})


def test_personal_schema_isolation_and_repository_scope_guard(owners):
    from database.repositories.research_notes_repository import ResearchNotesRepository
    alice, bob = owners
    shared_request = add()
    with user_scope(alice):
        first = ResearchNotesService(alice).save(**shared_request)
        repo = ResearchNotesRepository(alice)
    with user_scope(bob):
        service = ResearchNotesService(bob)
        assert service.read()["records"] == []
        with pytest.raises(ValueError, match="not_found"):
            service.save(**add(op="update", expected_revision=first["revision"]))
        with pytest.raises(ValueError, match="owner_mismatch"):
            repo.read(slug=None, limit=20, offset=0)
        # Identical UUIDs are separate requests in distinct personal schemas.
        assert service.save(**shared_request)["slug"] == first["slug"]
        assert service.read()["total"] == 1


def test_concurrent_replay_and_optimistic_update(owners):
    owner, _ = owners
    def save(value):
        with user_scope(owner):
            try:
                return ResearchNotesService(owner).save(**value)
            except ValueError as exc:
                return str(exc)
    first = add()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [first, first]))
    assert isinstance(results[0], dict) and results[0] == results[1]
    revision = results[0]["revision"]
    candidates = [add(op="update", expected_revision=revision, content=text) for text in ("研究 A", "研究 B")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, candidates))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert sum("revision_conflict" in row for row in results if isinstance(row, str)) == 1


def test_one_hundred_note_limit_is_atomic(owners):
    owner, _ = owners
    with user_scope(owner):
        service = ResearchNotesService(owner)
        for number in range(99):
            service.save(**add(slug=f"note-{number}"))
    def save(slug):
        with user_scope(owner):
            try:
                return ResearchNotesService(owner).save(**add(slug=slug))
            except ValueError as exc:
                return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["last-one", "last-two"]))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert "research_notes_limit_100" in results
    with user_scope(owner):
        page = service.read(limit=20, offset=80)
        assert page["total"] == 100 and len(page["records"]) == 20
