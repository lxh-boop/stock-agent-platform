from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from database.repositories.proposal_repository import ProposalRepository

from .models import ProposalArtifact, ProposalStatus


class ProposalStoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json(dict(payload or {})).encode("utf-8")).hexdigest()


def action_request_hash(
    *, proposal_id: str, proposal_version: int, payload_hash_value: str,
    action_type: str, user_id: str, session_id: str
) -> str:
    return hashlib.sha256(
        _json(
            {
                "proposal_id": str(proposal_id),
                "proposal_version": int(proposal_version),
                "payload_hash": str(payload_hash_value),
                "action_type": str(action_type),
                "user_id": str(user_id),
                "session_id": str(session_id),
            }
        ).encode("utf-8")
    ).hexdigest()


class ProposalStore:
    """Canonical Proposal lifecycle backed by the formal application database."""

    _ALLOWED_TRANSITIONS = {
        ProposalStatus.DRAFT: {ProposalStatus.PENDING_APPROVAL, ProposalStatus.CANCELLED},
        ProposalStatus.PENDING_APPROVAL: {
            ProposalStatus.APPROVED,
            ProposalStatus.REJECTED,
            ProposalStatus.CANCELLED,
            ProposalStatus.EXPIRED,
        },
        ProposalStatus.APPROVED: {ProposalStatus.EXECUTING, ProposalStatus.CANCELLED},
        ProposalStatus.EXECUTING: {ProposalStatus.EXECUTED, ProposalStatus.FAILED},
        ProposalStatus.EXECUTED: set(),
        ProposalStatus.FAILED: set(),
        ProposalStatus.REJECTED: set(),
        ProposalStatus.CANCELLED: set(),
        ProposalStatus.EXPIRED: set(),
    }

    def __init__(self, *, output_dir: str | Path = "outputs", db_path: str | Path | None = None) -> None:
        # Retained for the frozen Stage 6 caller signature. Proposal state is no
        # longer stored below outputs/.
        del output_dir
        del db_path
        self.repository = ProposalRepository()

    @staticmethod
    def deterministic_id(
        *, user_id: str, source_run_id: str, source_request_id: str, proposal_sequence: int = 1
    ) -> str:
        seed = "|".join(
            [
                str(user_id or "default"),
                str(source_run_id or ""),
                str(source_request_id or ""),
                str(max(1, int(proposal_sequence or 1))),
            ]
        )
        return "proposal_" + uuid5(NAMESPACE_URL, "agent-runtime-v23.0.17|" + seed).hex[:24]

    @staticmethod
    def _artifact(record: dict[str, Any]) -> ProposalArtifact:
        return ProposalArtifact(
            proposal_id=str(record["proposal_id"]),
            proposal_type=str(record["proposal_type"]),
            user_id=str(record["user_id"]),
            session_id=str(record["session_id"]),
            source_run_id=str(record["source_run_id"]),
            source_request_id=str(record["source_request_id"]),
            current_version=int(record["current_version"]),
            status=ProposalStatus.from_value(record["status"]),
            current_payload_hash=str(record["current_payload_hash"]),
            payload=dict(record.get("payload") or {}),
            approval_binding=dict(record.get("approval_binding") or {}),
            created_at=str(record["created_at"]),
            updated_at=str(record["updated_at"]),
            expires_at=str(record.get("expires_at") or ""),
            metadata=dict(record.get("metadata") or {}),
        )

    def _expire_due(self) -> None:
        self.repository.expire_due(now=_now())

    def create(
        self,
        *,
        proposal_type: str,
        user_id: str,
        session_id: str,
        source_run_id: str,
        source_request_id: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        expires_at: str = "",
        proposal_sequence: int = 1,
        created_by: str = "W05",
    ) -> ProposalArtifact:
        proposal_id = self.deterministic_id(
            user_id=user_id,
            source_run_id=source_run_id,
            source_request_id=source_request_id,
            proposal_sequence=proposal_sequence,
        )
        digest = payload_hash(payload)
        record, created = self.repository.create(
            proposal_id=proposal_id,
            proposal_type=str(proposal_type or "generic_proposal"),
            user_id=str(user_id or "default"),
            session_id=str(session_id or ""),
            source_run_id=str(source_run_id or ""),
            source_request_id=str(source_request_id or ""),
            payload_hash=digest,
            payload=dict(payload or {}),
            metadata=dict(metadata or {}),
            expires_at=str(expires_at or ""),
            created_by=str(created_by or "W05"),
            now=_now(),
        )
        if not created and str(record["current_payload_hash"]) != digest:
            raise ProposalStoreError("proposal_retry_payload_conflict")
        return self._artifact(record)

    def revise(
        self,
        *, proposal_id: str, user_id: str, payload: dict[str, Any],
        revision_reason: str = "", created_by: str = "W05",
    ) -> ProposalArtifact:
        self._expire_due()
        record, outcome = self.repository.revise(
            proposal_id=str(proposal_id or ""),
            user_id=str(user_id or "default"),
            payload_hash=payload_hash(payload),
            payload=dict(payload or {}),
            revision_reason=str(revision_reason or ""),
            created_by=str(created_by or "W05"),
            now=_now(),
            allowed_statuses=(ProposalStatus.DRAFT.value, ProposalStatus.PENDING_APPROVAL.value),
        )
        if outcome == "not_found":
            raise ProposalStoreError("proposal_not_found")
        if outcome == "owner_mismatch":
            raise ProposalStoreError("proposal_owner_mismatch")
        if outcome == "status_forbidden":
            raise ProposalStoreError(
                f"proposal_revision_forbidden:{(record or {}).get('status', 'unknown')}"
            )
        assert record is not None
        return self._artifact(record)

    def get(self, proposal_id: str) -> ProposalArtifact | None:
        self._expire_due()
        record = self.repository.get(str(proposal_id or ""))
        return self._artifact(record) if record else None

    def list_pending(
        self, *, user_id: str, session_id: str, limit: int = 20
    ) -> list[ProposalArtifact]:
        self._expire_due()
        records = self.repository.list_pending(
            user_id=str(user_id or "default"),
            session_id=str(session_id or ""),
            limit=max(1, min(100, int(limit or 20))),
        )
        return [self._artifact(record) for record in records]

    def list_pending_for_user(self, *, user_id: str, limit: int = 20) -> list[ProposalArtifact]:
        self._expire_due()
        records = self.repository.list_pending(
            user_id=str(user_id or "default"),
            session_id=None,
            limit=max(1, min(100, int(limit or 20))),
        )
        return [self._artifact(record) for record in records]

    def resolve_single_pending(self, *, user_id: str, session_id: str) -> ProposalArtifact | None:
        rows = self.list_pending(user_id=user_id, session_id=session_id, limit=2)
        return rows[0] if len(rows) == 1 else None

    def claim_for_execution(
        self,
        *, proposal_id: str, user_id: str, expected_version: int,
        expected_payload_hash: str, approved_by: str, approval_run_id: str,
    ) -> ProposalArtifact:
        self._expire_due()
        now = _now()
        binding = {
            "proposal_id": str(proposal_id),
            "proposal_version": int(expected_version),
            "payload_hash": str(expected_payload_hash or ""),
            "approved_by": str(approved_by or user_id),
            "approval_run_id": str(approval_run_id or ""),
            "approved_at": now,
        }
        record, outcome = self.repository.claim_execution(
            proposal_id=str(proposal_id or ""),
            user_id=str(user_id or "default"),
            expected_version=int(expected_version),
            expected_payload_hash=str(expected_payload_hash or ""),
            approval_binding=binding,
            now=now,
        )
        error_by_outcome = {
            "not_found": "proposal_not_found",
            "owner_mismatch": "proposal_owner_mismatch",
            "version_changed": "proposal_version_changed",
            "payload_hash_changed": "proposal_payload_hash_changed",
        }
        if outcome in error_by_outcome:
            raise ProposalStoreError(error_by_outcome[outcome])
        if outcome == "not_pending":
            raise ProposalStoreError(f"proposal_not_pending:{(record or {}).get('status', '')}")
        assert record is not None
        return self._artifact(record)

    def transition(
        self, *, proposal_id: str, user_id: str, target: ProposalStatus | str
    ) -> ProposalArtifact:
        target_status = ProposalStatus.from_value(target)
        current = self.get(str(proposal_id or ""))
        if current is None:
            raise ProposalStoreError("proposal_not_found")
        if current.user_id != str(user_id or "default"):
            raise ProposalStoreError("proposal_owner_mismatch")
        allowed_from = tuple(
            status.value
            for status, targets in self._ALLOWED_TRANSITIONS.items()
            if target_status in targets
        )
        record, outcome = self.repository.transition(
            proposal_id=current.proposal_id,
            user_id=str(user_id or "default"),
            target=target_status.value,
            allowed_from=allowed_from,
            now=_now(),
        )
        if outcome == "not_found":
            raise ProposalStoreError("proposal_not_found")
        if outcome == "owner_mismatch":
            raise ProposalStoreError("proposal_owner_mismatch")
        if outcome == "status_forbidden":
            status = str((record or {}).get("status") or current.status.value)
            raise ProposalStoreError(f"invalid_proposal_transition:{status}->{target_status.value}")
        assert record is not None
        return self._artifact(record)

    def begin_action(
        self,
        *, proposal: ProposalArtifact, action_type: str, user_id: str,
        session_id: str, idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        request_digest = action_request_hash(
            proposal_id=proposal.proposal_id,
            proposal_version=proposal.current_version,
            payload_hash_value=proposal.current_payload_hash,
            action_type=action_type,
            user_id=user_id,
            session_id=session_id,
        )
        action_request_id = "proposal_action_" + uuid5(
            NAMESPACE_URL,
            "agent-proposal-action|" + str(user_id) + "|" + str(idempotency_key),
        ).hex[:24]
        record, created = self.repository.begin_action(
            action_request_id=action_request_id,
            proposal_id=proposal.proposal_id,
            user_id=str(user_id or "default"),
            session_id=str(session_id or ""),
            action_type=str(action_type),
            idempotency_key=str(idempotency_key),
            request_hash=request_digest,
            now=_now(),
        )
        if not created and str(record.get("request_hash") or "") != request_digest:
            raise ProposalStoreError("idempotency_key_payload_conflict")
        return record, created

    def complete_action(self, *, action_request_id: str, result: dict[str, Any]) -> None:
        self.repository.complete_action(
            action_request_id=str(action_request_id),
            status="succeeded" if bool(result.get("success")) else "failed",
            result=dict(result),
            now=_now(),
        )


__all__ = [
    "ProposalStore",
    "ProposalStoreError",
    "action_request_hash",
    "payload_hash",
]
