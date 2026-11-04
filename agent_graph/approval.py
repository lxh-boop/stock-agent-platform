"""Write-approval helpers for the LangGraph engine.

Approval payloads are plain JSON dicts so they survive checkpointing and can be
replayed by the pending-actions API. Resume values are
``{"decision": "approved" | "rejected", "reason": str?}``.
"""

from __future__ import annotations

from typing import Any

APPROVAL_ACTION = "write_approval"

_DECISIONS = {"approved", "rejected"}


def decision_from_resume(resume_value: Any) -> str:
    if isinstance(resume_value, dict):
        decision = str(resume_value.get("decision") or "").strip().lower()
        if decision in _DECISIONS:
            return decision
    return "invalid"


def write_request_payload(requests: list[dict[str, Any]], *, user_id: str, session_id: str) -> dict[str, Any]:
    """Top-level interrupt payload for write-type decomposed requests."""

    return {
        "action": APPROVAL_ACTION,
        "gate": "write_request",
        "requests": requests,
        "summary": "；".join(str(r.get("objective") or "") for r in requests)[:500],
        "user_id": user_id,
        "session_id": session_id,
    }


__all__ = ["APPROVAL_ACTION", "decision_from_resume", "write_request_payload"]
