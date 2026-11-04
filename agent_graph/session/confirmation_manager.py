from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_graph.session.pending_action_store import get_pending_plan, save_pending_plan, update_pending_plan
from agent_graph.trace import trace_event
from database.repositories.agent_repository import AgentRepository


def _now() -> datetime:
    return datetime.now(UTC)


PLAN_HASH_EXCLUDED_FIELDS = {
    "confirmation_token",
    "confirmation_token_hash",
    "confirmation_status",
    "execution_status",
    "created_at",
    "expires_at",
    "confirmed_at",
    "used_at",
    "executed_at",
    "order_ids",
    "cash_flow_id",
    "backfill_status",
    "run_id",
}


def _json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in sorted(plan.items())
        if str(key) not in PLAN_HASH_EXCLUDED_FIELDS and str(key) != "plan_hash"
    }


def compute_plan_hash(plan: dict[str, Any]) -> str:
    return _json_hash(_stable_plan_payload(plan))


def compute_business_state_version(payload: dict[str, Any]) -> str:
    state = {
        "before": payload.get("before_state_summary")
        or payload.get("before")
        or {},
        "operation_type": payload.get("operation_type") or payload.get("intent") or "",
        "user_id": payload.get("user_id") or "",
    }
    return _json_hash(state)


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _persist_action_proposal(
    plan: dict[str, Any],
    *,
    db_path: str | Path | None,
    status: str | None = None,
) -> None:
    if db_path is None:
        return
    try:
        repository = AgentRepository(db_path)
        source_run_id = str(plan.get("run_id") or "")
        persisted_run_id = (
            source_run_id
            if source_run_id
            and repository.store.get(
                "agent_runs",
                {"run_id": source_run_id},
            )
            else None
        )
        repository.upsert_action_proposal(
            {
                "plan_id": str(plan.get("plan_id") or ""),
                "user_id": str(plan.get("user_id") or ""),
                "run_id": persisted_run_id,
                "operation_type": str(plan.get("operation_type") or plan.get("intent") or ""),
                "snapshot_id": str(plan.get("snapshot_id") or ""),
                "business_state_version": str(plan.get("business_state_version") or ""),
                "plan_hash": str(plan.get("plan_hash") or ""),
                "status": status or str(plan.get("confirmation_status") or "pending"),
                "created_at": str(plan.get("created_at") or ""),
                "expires_at": str(plan.get("expires_at") or ""),
                "before_state_summary": plan.get("before_state_summary") or plan.get("before") or {},
                "proposed_changes": plan.get("proposed_changes") or [],
                "after_state_preview": plan.get("after_state_preview") or plan.get("after") or {},
                "warnings": plan.get("warnings") or [],
                "validation_results": plan.get("validation_results") or {},
                "requires_confirmation": 1 if plan.get("requires_confirmation", True) else 0,
                "metadata": {
                    "intent": plan.get("intent"),
                    "execution_status": plan.get("execution_status"),
                    "source_run_id": source_run_id,
                },
            }
        )
    except Exception:
        # Audit persistence must not block the existing protected business gateway.
        return


def _persist_action_approval(
    plan: dict[str, Any] | None,
    *,
    db_path: str | Path | None,
    status: str,
    approval_id: str | None = None,
) -> str:
    if db_path is None or not plan:
        return approval_id or ""
    approval_id = approval_id or f"approval_{uuid4().hex[:12]}"
    try:
        AgentRepository(db_path).upsert_action_approval(
            {
                "approval_id": approval_id,
                "plan_id": str(plan.get("plan_id") or ""),
                "user_id": str(plan.get("user_id") or ""),
                "plan_hash": str(plan.get("plan_hash") or ""),
                "snapshot_id": str(plan.get("snapshot_id") or ""),
                "business_state_version": str(plan.get("business_state_version") or ""),
                "status": status,
                "created_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "used_at": _now().strftime("%Y-%m-%d %H:%M:%S") if status == "confirmed" else "",
                "expires_at": str(plan.get("expires_at") or ""),
                "metadata": {"intent": plan.get("intent"), "operation_type": plan.get("operation_type")},
            }
        )
    except Exception:
        return approval_id
    return approval_id


def persist_action_commit(
    plan: dict[str, Any] | None,
    *,
    db_path: str | Path | None,
    status: str,
    result_summary: dict[str, Any] | None = None,
    error_type: str = "",
    error_message: str = "",
) -> str:
    if db_path is None or not plan:
        return ""
    plan_id = str(plan.get("plan_id") or "")
    commit_id = f"commit_{plan_id}"
    approval_id = str(plan.get("approval_id") or "")
    before_state = plan.get("before_state_summary") or plan.get("before") or {}
    after_state = plan.get("after_state_preview") or plan.get("after") or {}
    try:
        AgentRepository(db_path).upsert_action_commit(
            {
                "commit_id": commit_id,
                "plan_id": plan_id,
                "approval_id": approval_id or None,
                "user_id": str(plan.get("user_id") or ""),
                "status": status,
                "idempotency_key": f"{plan_id}:commit_once",
                "before_state_hash": _json_hash(before_state),
                "after_state_hash": _json_hash(after_state),
                "result_summary": result_summary or {},
                "error_type": str(error_type or ""),
                "error_message": str(error_message or "")[:500],
                "created_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
                "committed_at": _now().strftime("%Y-%m-%d %H:%M:%S") if status == "executed" else "",
                "metadata": {"intent": plan.get("intent"), "operation_type": plan.get("operation_type")},
            }
        )
    except Exception:
        return commit_id
    return commit_id


def create_confirmation_plan(
    user_id: str,
    intent: str,
    payload: dict[str, Any],
    output_dir: str | Path = "outputs",
    ttl_minutes: int = 30,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    plan_id = payload.get("plan_id") or f"agent_plan_{uuid4().hex[:12]}"
    token = secrets.token_urlsafe(16)
    expires_at = (_now() + timedelta(minutes=int(ttl_minutes))).strftime("%Y-%m-%d %H:%M:%S")
    plan = {
        **payload,
        "plan_id": plan_id,
        "user_id": user_id,
        "intent": intent,
        "operation_type": payload.get("operation_type") or intent,
        "snapshot_id": payload.get("snapshot_id") or f"snapshot_{uuid4().hex[:12]}",
        "business_state_version": payload.get("business_state_version") or "",
        "before_state_summary": payload.get("before_state_summary") or payload.get("before") or {},
        "proposed_changes": payload.get("proposed_changes") or [],
        "after_state_preview": payload.get("after_state_preview") or payload.get("after") or {},
        "warnings": payload.get("warnings") or [],
        "validation_results": payload.get("validation_results") or {},
        "requires_confirmation": True,
        "confirmation_token": token,
        "confirmation_token_hash": _token_hash(token),
        "confirmation_status": "pending",
        "execution_status": "pending",
        "expires_at": expires_at,
        "created_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not plan.get("business_state_version"):
        plan["business_state_version"] = compute_business_state_version(plan)
    plan["plan_hash"] = payload.get("plan_hash") or compute_plan_hash(plan)
    _persist_action_proposal(plan, db_path=db_path, status="pending")
    trace_event(
        "approval.requested",
        {
            "plan_id": plan.get("plan_id"),
            "plan_hash": plan.get("plan_hash"),
            "intent": plan.get("intent"),
            "operation_type": plan.get("operation_type"),
            "status": plan.get("confirmation_status"),
            "token_present": bool(plan.get("confirmation_token")),
            "proposed_change_count": len(plan.get("proposed_changes") or []),
            "warning_count": len(plan.get("warnings") or []),
            "plan_ref": {
                "plan_id": plan.get("plan_id"),
                "plan_hash": plan.get("plan_hash"),
                "status": plan.get("confirmation_status"),
                "token_present": bool(plan.get("confirmation_token")),
            },
        },
        run_id=str(plan.get("run_id") or ""),
    )
    return save_pending_plan(user_id, plan, output_dir)


def validate_confirmation(
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    ok, status, plan = inspect_confirmation(
        user_id,
        plan_id,
        confirmation_token,
        output_dir=output_dir,
        db_path=db_path,
        record_failure=True,
    )
    if not ok or not plan:
        return ok, status, plan
    approval_id = _persist_action_approval(plan, db_path=db_path, status="confirmed")
    plan = update_pending_plan(
        user_id,
        plan_id,
        {
            "confirmation_status": "confirmed",
            "used_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "approval_id": approval_id,
        },
        output_dir,
    )
    _persist_action_proposal(plan, db_path=db_path, status="confirmed")
    return True, "confirmed", plan


def inspect_confirmation(
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    *,
    record_failure: bool = False,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Validate identity, integrity and expiry without consuming approval.

    Business-state revalidation must run after this check and before
    ``validate_confirmation`` records the confirmed approval.
    """
    plan = get_pending_plan(user_id, plan_id, output_dir)
    if not plan:
        return False, "plan_not_found", None
    if plan.get("execution_status") == "executed":
        if record_failure:
            _persist_action_approval(plan, db_path=db_path, status="already_executed")
        return False, "already_executed", plan
    if plan.get("execution_status") == "rejected" or plan.get("confirmation_status") == "rejected":
        if record_failure:
            _persist_action_approval(plan, db_path=db_path, status="plan_rejected")
        return False, "plan_rejected", plan
    if plan.get("confirmation_status") == "confirmed":
        if record_failure:
            _persist_action_approval(plan, db_path=db_path, status="confirmation_already_used")
        return False, "confirmation_already_used", plan
    if str(plan.get("confirmation_token") or "") != str(confirmation_token or ""):
        if record_failure:
            _persist_action_approval(plan, db_path=db_path, status="invalid_confirmation_token")
        return False, "invalid_confirmation_token", plan
    if str(plan.get("plan_hash") or "") != compute_plan_hash(plan):
        update_pending_plan(user_id, plan_id, {"confirmation_status": "invalid", "execution_status": "rejected"}, output_dir)
        _persist_action_proposal({**plan, "confirmation_status": "invalid"}, db_path=db_path, status="invalid")
        if record_failure:
            _persist_action_approval(plan, db_path=db_path, status="plan_hash_mismatch")
        return False, "plan_hash_mismatch", plan
    expires = datetime.strptime(str(plan.get("expires_at")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    if _now() > expires:
        update_pending_plan(user_id, plan_id, {"confirmation_status": "expired"}, output_dir)
        _persist_action_proposal({**plan, "confirmation_status": "expired"}, db_path=db_path, status="expired")
        if record_failure:
            _persist_action_approval(plan, db_path=db_path, status="expired")
        return False, "confirmation_token_expired", plan
    return True, "confirmation_valid", plan


def reject_confirmation_plan(
    user_id: str,
    plan_id: str,
    *,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    reason: str = "user_rejected",
) -> tuple[bool, str, dict[str, Any] | None]:
    plan = get_pending_plan(user_id, plan_id, output_dir)
    if not plan:
        return False, "plan_not_found", None
    if plan.get("execution_status") == "executed":
        return False, "already_executed", plan
    if plan.get("execution_status") == "rejected":
        return False, "already_rejected", plan
    updated = update_pending_plan(
        user_id,
        plan_id,
        {
            "confirmation_status": "rejected",
            "execution_status": "rejected",
            "rejected_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            "rejected_reason": str(reason or "user_rejected")[:200],
        },
        output_dir,
    )
    _persist_action_proposal(updated, db_path=db_path, status="rejected")
    _persist_action_approval(updated, db_path=db_path, status="rejected")
    return True, "rejected", updated


def mark_plan_revalidation_failed(
    user_id: str,
    plan_id: str,
    *,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    reason: str,
) -> dict[str, Any] | None:
    plan = get_pending_plan(user_id, plan_id, output_dir)
    if not plan:
        return None
    updated = update_pending_plan(
        user_id,
        plan_id,
        {
            "execution_status": "rejected",
            "rejected_reason": str(reason or "revalidation_failed")[:200],
        },
        output_dir,
    )
    _persist_action_proposal(updated, db_path=db_path, status="revalidation_failed")
    return updated


def mark_plan_executed(
    user_id: str,
    plan_id: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    **changes: Any,
) -> dict[str, Any]:
    plan = update_pending_plan(
        user_id,
        plan_id,
        {
            "confirmation_status": "confirmed",
            "execution_status": "executed",
            "executed_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
            **changes,
        },
        output_dir,
    )
    _persist_action_proposal(plan, db_path=db_path, status="executed")
    persist_action_commit(
        plan,
        db_path=db_path,
        status="executed",
        result_summary={key: value for key, value in changes.items() if key in {"order_ids", "cash_flow_id", "backfill_status", "execution_status"}},
    )
    return plan
