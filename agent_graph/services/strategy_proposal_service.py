from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import uuid
from pathlib import Path
from typing import Any

from database.repositories.strategy_workflow_repository import (
    ACTIVE_PROPOSAL_STATUSES,
    StrategyWorkflowRepository,
)


VALID_PROPOSAL_STATUSES = {
    *ACTIVE_PROPOSAL_STATUSES,
    "cancelled",
    "superseded",
}


def _now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class StrategyProposal:
    proposal_id: str
    user_id: str
    account_id: str
    conversation_id: str
    original_request: str
    current_version: int
    status: str
    created_at: str
    updated_at: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyProposal":
        return cls(
            proposal_id=str(data.get("proposal_id") or ""),
            user_id=str(data.get("user_id") or ""),
            account_id=str(data.get("account_id") or ""),
            conversation_id=str(data.get("conversation_id") or ""),
            original_request=str(data.get("original_request") or ""),
            current_version=int(data.get("current_version") or 0),
            status=str(data.get("status") or "draft"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyProposalVersion:
    proposal_id: str
    version: int
    base_strategy_id: str
    base_strategy_version: str
    proposal_json: dict[str, Any]
    user_feedback: str = ""
    change_summary: str = ""
    created_at: str = ""
    source_run_id: str = ""

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
    ) -> "StrategyProposalVersion":
        return cls(
            proposal_id=str(data.get("proposal_id") or ""),
            version=int(data.get("version") or 0),
            base_strategy_id=str(data.get("base_strategy_id") or ""),
            base_strategy_version=str(
                data.get("base_strategy_version") or ""
            ),
            proposal_json=dict(data.get("proposal_json") or {}),
            user_feedback=str(data.get("user_feedback") or ""),
            change_summary=str(data.get("change_summary") or ""),
            created_at=str(data.get("created_at") or ""),
            source_run_id=str(data.get("source_run_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyConversationContext:
    user_id: str
    account_id: str
    conversation_id: str
    current_account: dict[str, Any] = field(default_factory=dict)
    current_positions: list[dict[str, Any]] = field(default_factory=list)
    current_strategy: dict[str, Any] = field(default_factory=dict)
    strategy_capabilities: dict[str, Any] = field(default_factory=dict)
    user_constraints: dict[str, Any] = field(default_factory=dict)
    related_conversation: list[dict[str, Any]] = field(default_factory=list)
    active_proposal: dict[str, Any] = field(default_factory=dict)
    proposal_version_history: list[dict[str, Any]] = field(
        default_factory=list
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyProposalService:
    """Persist LLM-authored strategy drafts without interpreting their meaning."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.repository = StrategyWorkflowRepository(db_path)

    def get(
        self,
        proposal_id: str,
        *,
        user_id: str,
    ) -> StrategyProposal | None:
        row = self.repository.get_proposal(
            proposal_id,
            user_id=user_id,
        )
        return StrategyProposal.from_dict(row) if row else None

    def get_active(
        self,
        *,
        user_id: str,
        account_id: str,
        conversation_id: str,
    ) -> StrategyProposal | None:
        row = self.repository.get_active_proposal(
            user_id=user_id,
            account_id=account_id,
            conversation_id=conversation_id,
        )
        return StrategyProposal.from_dict(row) if row else None

    def list_versions(
        self,
        proposal_id: str,
        *,
        user_id: str,
    ) -> list[StrategyProposalVersion]:
        if self.get(proposal_id, user_id=user_id) is None:
            return []
        return [
            StrategyProposalVersion.from_dict(row)
            for row in self.repository.list_versions(proposal_id)
        ]

    def save_draft(
        self,
        *,
        user_id: str,
        account_id: str,
        conversation_id: str,
        original_request: str,
        proposal_json: dict[str, Any],
        base_strategy_id: str,
        base_strategy_version: str,
        user_feedback: str = "",
        change_summary: str = "",
        source_run_id: str = "",
        proposal_id: str = "",
    ) -> tuple[StrategyProposal, StrategyProposalVersion]:
        if not isinstance(proposal_json, dict) or not proposal_json:
            raise ValueError("proposal_json_must_be_non_empty_object")
        if proposal_id:
            proposal = self.get(proposal_id, user_id=user_id)
            if proposal is None:
                raise ValueError("proposal_not_found")
            if (
                proposal.account_id != account_id
                or proposal.conversation_id != conversation_id
            ):
                raise PermissionError("proposal_scope_mismatch")
        else:
            proposal = self.get_active(
                user_id=user_id,
                account_id=account_id,
                conversation_id=conversation_id,
            )

        now = _now_text()
        if proposal is None:
            proposal_id = f"strategy_proposal_{uuid.uuid4().hex}"
            proposal = StrategyProposal(
                proposal_id=proposal_id,
                user_id=user_id,
                account_id=account_id,
                conversation_id=conversation_id,
                original_request=str(original_request or ""),
                current_version=1,
                status="draft",
                created_at=now,
                updated_at=now,
            )
            self.repository.create_proposal(proposal.to_dict())
            version_number = 1
        else:
            version_number = proposal.current_version + 1
            self.repository.update_proposal(
                proposal.proposal_id,
                {
                    "current_version": version_number,
                    "status": "revising",
                    "updated_at": now,
                },
            )
            proposal = StrategyProposal(
                **{
                    **proposal.to_dict(),
                    "current_version": version_number,
                    "status": "revising",
                    "updated_at": now,
                }
            )

        version = StrategyProposalVersion(
            proposal_id=proposal.proposal_id,
            version=version_number,
            base_strategy_id=str(base_strategy_id or ""),
            base_strategy_version=str(base_strategy_version or ""),
            proposal_json=dict(proposal_json),
            user_feedback=str(user_feedback or ""),
            change_summary=str(change_summary or ""),
            created_at=now,
            source_run_id=str(source_run_id or ""),
        )
        self.repository.insert_version(version.to_dict())
        return proposal, version

    def set_status(
        self,
        proposal_id: str,
        *,
        user_id: str,
        status: str,
        expected_version: int | None = None,
    ) -> StrategyProposal:
        if status not in VALID_PROPOSAL_STATUSES:
            raise ValueError("invalid_proposal_status")
        proposal = self.get(proposal_id, user_id=user_id)
        if proposal is None:
            raise ValueError("proposal_not_found")
        if (
            expected_version is not None
            and proposal.current_version != int(expected_version)
        ):
            raise ValueError("stale_proposal_version")
        now = _now_text()
        self.repository.update_proposal(
            proposal_id,
            {"status": status, "updated_at": now},
        )
        return StrategyProposal(
            **{
                **proposal.to_dict(),
                "status": status,
                "updated_at": now,
            }
        )
