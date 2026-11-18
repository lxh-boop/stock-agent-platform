"""Personal research notes; no filesystem or shared-profile persistence."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from core.user_scope import current_owner, user_schema, validate_owner
from database.connection import transaction


class ResearchNotesRepository:
    def __init__(self, user_id: str) -> None:
        self.user_id = validate_owner(user_id)

    def _scope(self) -> str:
        if current_owner() != self.user_id:
            raise ValueError("research_notes_authenticated_owner_mismatch")
        return user_schema(self.user_id)

    def _tables(self, conn):
        schema = self._scope()
        notes = sql.Identifier(schema, "research_notes")
        requests = sql.Identifier(schema, "research_note_requests")
        revision = sql.Identifier(schema, "research_note_revision")
        # Serialize creation, the 100-note quota and every mutation for this owner.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("research-notes:" + schema,))
        conn.execute(sql.SQL("CREATE SEQUENCE IF NOT EXISTS {}").format(revision))
        conn.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
            slug TEXT PRIMARY KEY CHECK (slug ~ '^[a-z][a-z0-9]*(-[a-z0-9]+)*$' AND char_length(slug) <= 64),
            title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 100),
            content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 2000),
            revision BIGINT NOT NULL CHECK (revision > 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""").format(notes))
        conn.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
            request_id UUID PRIMARY KEY,
            request_hash TEXT NOT NULL,
            result JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""").format(requests))
        return notes, requests, revision

    def read(self, *, slug: str | None, limit: int, offset: int) -> dict[str, Any]:
        self._scope()
        with transaction() as conn:
            notes, _, _ = self._tables(conn)
            where = sql.SQL("WHERE slug=%s") if slug else sql.SQL("")
            params = (slug,) if slug else ()
            total = conn.execute(sql.SQL("SELECT COUNT(*) AS n FROM {} {}").format(notes, where), params).fetchone()["n"]
            rows = conn.execute(sql.SQL("""SELECT slug, title, content, revision, created_at, updated_at
                FROM {} {} ORDER BY slug LIMIT %s OFFSET %s""").format(notes, where), (*params, limit, offset)).fetchall()
        records = [{**row, "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()} for row in rows]
        return {"records": records, "total": total, "limit": limit, "offset": offset}

    def mutate(self, *, op: str, slug: str, title: str, content: str,
               expected_revision: int | None, request_id: UUID, request_hash: str) -> dict[str, Any]:
        self._scope()
        with transaction() as conn:
            notes, requests, revision_seq = self._tables(conn)
            previous = conn.execute(sql.SQL("SELECT request_hash, result FROM {} WHERE request_id=%s").format(requests), (request_id,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ValueError("research_note_request_id_conflict")
                return dict(previous["result"])
            existing = conn.execute(sql.SQL("SELECT revision FROM {} WHERE slug=%s FOR UPDATE").format(notes), (slug,)).fetchone()
            if op == "add":
                if existing:
                    raise ValueError("research_note_already_exists")
                count = conn.execute(sql.SQL("SELECT COUNT(*) AS n FROM {}").format(notes)).fetchone()["n"]
                if count >= 100:
                    raise ValueError("research_notes_limit_100")
            elif op in {"update", "delete"}:
                if not existing:
                    raise ValueError("research_note_not_found")
                if existing["revision"] != expected_revision:
                    raise ValueError("research_note_revision_conflict_read_again")
            else:
                raise ValueError("invalid_research_note_operation")

            if op == "delete":
                conn.execute(sql.SQL("DELETE FROM {} WHERE slug=%s AND revision=%s").format(notes), (slug, expected_revision))
                revision = expected_revision
            else:
                # A schema-local sequence prevents stale revisions from matching a
                # deleted and subsequently recreated slug (the ABA problem).
                revision = conn.execute(sql.SQL("SELECT nextval({}::regclass) AS n").format(sql.Literal(revision_seq.as_string(conn)))).fetchone()["n"]
                if op == "add":
                    conn.execute(sql.SQL("INSERT INTO {} (slug,title,content,revision) VALUES (%s,%s,%s,%s)").format(notes), (slug, title, content, revision))
                else:
                    conn.execute(sql.SQL("UPDATE {} SET title=%s,content=%s,revision=%s,updated_at=CURRENT_TIMESTAMP WHERE slug=%s AND revision=%s").format(notes), (title, content, revision, slug, expected_revision))
            result = {"op": op, "slug": slug, "revision": revision, "deleted": op == "delete", "request_id": str(request_id)}
            # Persist only the operation receipt, not another copy of note content.
            conn.execute(sql.SQL("INSERT INTO {} (request_id,request_hash,result) VALUES (%s,%s,%s)").format(requests), (request_id, request_hash, Jsonb(result)))
            return result
