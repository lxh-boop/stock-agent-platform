from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @classmethod
    def from_value(cls, value: Any) -> "ProposalStatus":
        text = str(getattr(value, "value", value) or "").strip().lower()
        for item in cls:
            if item.value == text:
                return item
        raise ValueError(f"invalid_proposal_status:{text}")


@dataclass(frozen=True)
class ProposalArtifact:
    proposal_id: str
    proposal_type: str
    user_id: str
    session_id: str
    source_run_id: str
    source_request_id: str
    current_version: int
    status: ProposalStatus
    current_payload_hash: str
    payload: dict[str, Any] = field(default_factory=dict)
    approval_binding: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["status"] = self.status.value
        return row
