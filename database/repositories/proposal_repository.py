from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Iterator, Sequence

from database.connection import get_connection


def _json(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _dict(row: dict[str, Any] | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class ProposalRepository:
    """PostgreSQL persistence boundary for the canonical Agent Proposal runtime."""

    def __init__(self) -> None:
        pass

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[object]:
        del immediate
        conn = get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _hydrate(conn, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        record = dict(row)
        version = conn.execute(
            """SELECT payload_json FROM proposal_versions
               WHERE proposal_id=%s AND version=%s""",
            (record["proposal_id"], int(record["current_version"])),
        ).fetchone()
        record["payload"] = json.loads(str(version["payload_json"] or "{}")) if version else {}
        record["approval_binding"] = json.loads(str(record.pop("approval_binding_json", "{}") or "{}"))
        record["metadata"] = json.loads(str(record.pop("metadata_json", "{}") or "{}"))
        return record

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id=%s",
                (str(proposal_id or ""),),
            ).fetchone()
            return self._hydrate(conn, row)

    def list_pending(
        self,
        *,
        user_id: str,
        session_id: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if session_id is None:
                rows = conn.execute(
                    """SELECT * FROM proposals
                       WHERE user_id=%s AND status='pending_approval'
                       ORDER BY updated_at DESC LIMIT %s""",
                    (str(user_id), int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM proposals
                       WHERE user_id=%s AND session_id=%s AND status='pending_approval'
                       ORDER BY updated_at DESC LIMIT %s""",
                    (str(user_id), str(session_id), int(limit)),
                ).fetchall()
            return [record for row in rows if (record := self._hydrate(conn, row)) is not None]

    def expire_due(self, *, now: str) -> int:
        with self._connect(immediate=True) as conn:
            cursor = conn.execute(
                """UPDATE proposals SET status='expired', updated_at=%s
                   WHERE status='pending_approval' AND expires_at<>''
                     AND NULLIF(expires_at, '')::timestamptz <= %s::timestamptz""",
                (str(now), str(now)),
            )
            return int(cursor.rowcount or 0)

    def create(
        self,
        *,
        proposal_id: str,
        proposal_type: str,
        user_id: str,
        session_id: str,
        source_run_id: str,
        source_request_id: str,
        payload_hash: str,
        payload: dict[str, Any],
        metadata: dict[str, Any],
        expires_at: str,
        created_by: str,
        now: str,
    ) -> tuple[dict[str, Any], bool]:
        with self._connect(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM proposals WHERE proposal_id=%s",
                (str(proposal_id),),
            ).fetchone()
            if existing is not None:
                hydrated = self._hydrate(conn, existing)
                assert hydrated is not None
                return hydrated, False
            conn.execute(
                """INSERT INTO proposals (
                    proposal_id, proposal_type, user_id, session_id, source_run_id,
                    source_request_id, current_version, status, current_payload_hash,
                    approval_binding_json, created_at, updated_at, expires_at, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, 1, 'pending_approval', %s, '{}', %s, %s, %s, %s)""",
                (
                    proposal_id,
                    proposal_type,
                    user_id,
                    session_id,
                    source_run_id,
                    source_request_id,
                    payload_hash,
                    now,
                    now,
                    expires_at,
                    _json(metadata),
                ),
            )
            conn.execute(
                """INSERT INTO proposal_versions (
                    proposal_id, version, payload_hash, payload_json, created_at,
                    created_by, revision_reason, base_version, metadata_json
                ) VALUES (%s, 1, %s, %s, %s, %s, '', 0, '{}')""",
                (proposal_id, payload_hash, _json(payload), now, created_by),
            )
            row = conn.execute("SELECT * FROM proposals WHERE proposal_id=%s", (proposal_id,)).fetchone()
            hydrated = self._hydrate(conn, row)
            assert hydrated is not None
            return hydrated, True

    def revise(
        self,
        *,
        proposal_id: str,
        user_id: str,
        payload_hash: str,
        payload: dict[str, Any],
        revision_reason: str,
        created_by: str,
        now: str,
        allowed_statuses: Sequence[str],
    ) -> tuple[dict[str, Any] | None, str]:
        with self._connect(immediate=True) as conn:
            row = conn.execute("SELECT * FROM proposals WHERE proposal_id=%s", (proposal_id,)).fetchone()
            if row is None:
                return None, "not_found"
            if str(row["user_id"]) != str(user_id):
                return self._hydrate(conn, row), "owner_mismatch"
            if str(row["status"]) not in set(allowed_statuses):
                return self._hydrate(conn, row), "status_forbidden"
            if str(row["current_payload_hash"]) == str(payload_hash):
                return self._hydrate(conn, row), "unchanged"
            current_version = int(row["current_version"])
            next_version = current_version + 1
            conn.execute(
                """INSERT INTO proposal_versions (
                    proposal_id, version, payload_hash, payload_json, created_at,
                    created_by, revision_reason, base_version, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '{}')""",
                (
                    proposal_id,
                    next_version,
                    payload_hash,
                    _json(payload),
                    now,
                    created_by,
                    revision_reason,
                    current_version,
                ),
            )
            conn.execute(
                """UPDATE proposals SET current_version=%s, current_payload_hash=%s,
                   status='pending_approval', approval_binding_json='{}', updated_at=%s
                   WHERE proposal_id=%s""",
                (next_version, payload_hash, now, proposal_id),
            )
            updated = conn.execute("SELECT * FROM proposals WHERE proposal_id=%s", (proposal_id,)).fetchone()
            return self._hydrate(conn, updated), "revised"

    def transition(
        self,
        *,
        proposal_id: str,
        user_id: str,
        target: str,
        allowed_from: Sequence[str],
        now: str,
    ) -> tuple[dict[str, Any] | None, str]:
        with self._connect(immediate=True) as conn:
            row = conn.execute("SELECT * FROM proposals WHERE proposal_id=%s", (proposal_id,)).fetchone()
            if row is None:
                return None, "not_found"
            if str(row["user_id"]) != str(user_id):
                return self._hydrate(conn, row), "owner_mismatch"
            if str(row["status"]) == str(target):
                return self._hydrate(conn, row), "unchanged"
            if str(row["status"]) not in set(allowed_from):
                return self._hydrate(conn, row), "status_forbidden"
            conn.execute(
                "UPDATE proposals SET status=%s, updated_at=%s WHERE proposal_id=%s",
                (target, now, proposal_id),
            )
            updated = conn.execute("SELECT * FROM proposals WHERE proposal_id=%s", (proposal_id,)).fetchone()
            return self._hydrate(conn, updated), "transitioned"

    def claim_execution(
        self,
        *,
        proposal_id: str,
        user_id: str,
        expected_version: int,
        expected_payload_hash: str,
        approval_binding: dict[str, Any],
        now: str,
    ) -> tuple[dict[str, Any] | None, str]:
        with self._connect(immediate=True) as conn:
            row = conn.execute("SELECT * FROM proposals WHERE proposal_id=%s", (proposal_id,)).fetchone()
            if row is None:
                return None, "not_found"
            hydrated = self._hydrate(conn, row)
            if str(row["user_id"]) != str(user_id):
                return hydrated, "owner_mismatch"
            if str(row["status"]) != "pending_approval":
                return hydrated, "not_pending"
            if int(row["current_version"]) != int(expected_version):
                return hydrated, "version_changed"
            if str(row["current_payload_hash"]) != str(expected_payload_hash):
                return hydrated, "payload_hash_changed"
            conn.execute(
                """UPDATE proposals SET status='executing', approval_binding_json=%s, updated_at=%s
                   WHERE proposal_id=%s""",
                (_json(approval_binding), now, proposal_id),
            )
            updated = conn.execute("SELECT * FROM proposals WHERE proposal_id=%s", (proposal_id,)).fetchone()
            return self._hydrate(conn, updated), "claimed"

    def get_action(self, *, user_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM proposal_action_requests
                   WHERE user_id=%s AND idempotency_key=%s""",
                (user_id, idempotency_key),
            ).fetchone()
            record = _dict(row)
            if record is not None:
                record["result"] = json.loads(str(record.pop("result_json", "{}") or "{}"))
            return record

    def begin_action(
        self,
        *,
        action_request_id: str,
        proposal_id: str,
        user_id: str,
        session_id: str,
        action_type: str,
        idempotency_key: str,
        request_hash: str,
        now: str,
    ) -> tuple[dict[str, Any], bool]:
        with self._connect(immediate=True) as conn:
            existing = conn.execute(
                """SELECT * FROM proposal_action_requests
                   WHERE user_id=%s AND idempotency_key=%s""",
                (user_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                record = dict(existing)
                record["result"] = json.loads(str(record.pop("result_json", "{}") or "{}"))
                return record, False
            conn.execute(
                """INSERT INTO proposal_action_requests (
                    action_request_id, proposal_id, user_id, session_id, action_type,
                    idempotency_key, request_hash, status, result_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'in_progress', '{}', %s, %s)""",
                (
                    action_request_id,
                    proposal_id,
                    user_id,
                    session_id,
                    action_type,
                    idempotency_key,
                    request_hash,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM proposal_action_requests WHERE action_request_id=%s",
                (action_request_id,),
            ).fetchone()
            record = dict(row)
            record["result"] = json.loads(str(record.pop("result_json", "{}") or "{}"))
            return record, True

    def complete_action(
        self,
        *,
        action_request_id: str,
        status: str,
        result: dict[str, Any],
        now: str,
    ) -> None:
        with self._connect(immediate=True) as conn:
            conn.execute(
                """UPDATE proposal_action_requests
                   SET status=%s, result_json=%s, updated_at=%s, completed_at=%s
                   WHERE action_request_id=%s""",
                (status, _json(result), now, now, action_request_id),
            )
