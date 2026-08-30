"""Redacted, durable evidence for real LLM calls made by the Agent.

The module deliberately records transport metadata only.  Prompts, responses,
credentials and local paths never enter the audit stream.
"""

from __future__ import annotations

import contextvars
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.console_trace import append_unified_event, sanitize_for_trace, unified_event_path


_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "agent_llm_audit_context", default={}
)
_LOCK = threading.RLock()
_ALLOWED_STAGES = {
    "planner",
    "goal_reviewer",
    "plan_reviewer",
    "completion",
    "report",
    "critic",
    "replan",
    "main_agent_single_entry",
    "graph_entity_candidate_extraction",
    "graph_coordinator_planner",
    "graph_coordinator_forward_replan",
    "worker_tool_dag_planner",
    "worker_tool_dag_replanner",
    "graph_entity_analysis",
    "graph_risk_analyst",
    "graph_strategy_guard",
    "graph_report_writer",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def activate_llm_audit_context(
    *,
    run_id: str,
    conversation_id: str,
    output_dir: str | Path | None,
    case_id: str = "",
    iteration: int | None = None,
    formal_entry_used: bool = False,
    formal_entry_name: str = "",
    task_id: str = "",
    worker_id: str = "",
    agent_id: str = "",
) -> None:
    """Bind executor-owned metadata to subsequent LLM calls in this context."""
    _CONTEXT.set(
        {
            "run_id": str(run_id or ""),
            "conversation_id": str(conversation_id or ""),
            "output_dir": str(output_dir or ""),
            "case_id": str(case_id or ""),
            "iteration": iteration,
            "formal_entry_used": bool(formal_entry_used),
            "formal_entry_name": str(formal_entry_name or ""),
            "task_id": str(task_id or ""),
            "worker_id": str(worker_id or ""),
            "agent_id": str(agent_id or ""),
        }
    )


def _event_path(context: dict[str, Any]) -> Path | None:
    run_id = str(context.get("run_id") or "").strip()
    output_dir = str(context.get("output_dir") or "").strip()
    if not run_id:
        return None
    return unified_event_path(run_id, output_dir or None)


def record_llm_call(
    *,
    stage: str,
    provider: str,
    model: str,
    temperature: float,
    request_at: str,
    response_at: str,
    duration_ms: int,
    success: bool,
    http_status: int | None = None,
    provider_request_id: str | None = None,
    error_type: str = "",
    error_message: str = "",
    operation: str = "",
    deployment_mode: str = "api",
    profile_id: str = "",
    config_hash: str = "",
    endpoint_scope: str = "remote",
    prompt_chars: int = 0,
    max_output_tokens: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    reasoning_tokens: int = 0,
    timing: dict[str, Any] | None = None,
    thinking_disabled: bool = False,
) -> str:
    """Persist one actual transport attempt when invoked by the formal executor."""
    context = dict(_CONTEXT.get() or {})
    path = _event_path(context)
    if path is None:
        return ""
    normalized_stage = str(stage or "").strip().lower()
    if normalized_stage not in _ALLOWED_STAGES:
        normalized_stage = "completion"
    event_id = f"llm_{uuid4().hex}"
    event = {
        "event_type": "LLM_CALL",
        "event_id": event_id,
        "run_id": str(context.get("run_id") or ""),
        "conversation_id": str(context.get("conversation_id") or ""),
        "case_id": str(context.get("case_id") or ""),
        "iteration": context.get("iteration"),
        "task_id": str(context.get("task_id") or ""),
        "worker_id": str(context.get("worker_id") or ""),
        "agent_id": str(context.get("agent_id") or ""),
        "stage": normalized_stage,
        "raw_stage": str(stage or "")[:120],
        "covered_stages": (
            ["goal_reviewer", "plan_reviewer"]
            if str(operation or "") == "goal_and_plan_review"
            else [normalized_stage]
        ),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "deployment_mode": str(deployment_mode or "api"),
        "profile_id": str(profile_id or "")[:200] or None,
        "config_hash": str(config_hash or "")[:64] or None,
        "endpoint_scope": str(endpoint_scope or "remote"),
        "temperature": float(temperature),
        "request_at": request_at,
        "response_at": response_at,
        "duration_ms": max(0, int(duration_ms)),
        "success": bool(success),
        "http_status": int(http_status) if isinstance(http_status, int) else None,
        "provider_request_id": str(provider_request_id or "")[:200] or None,
        "response_schema_valid": None,
        "error_type": str(error_type or "")[:120] or None,
        "error_message": str(error_message or "")[:500] or None,
        "fallback_used": False,
        "mock_used": False,
        "formal_entry_used": bool(context.get("formal_entry_used")),
        "formal_entry_name": str(context.get("formal_entry_name") or ""),
        "operation": str(operation or "")[:80] or None,
        "prompt_chars": max(0, int(prompt_chars or 0)),
        "max_output_tokens": max(0, int(max_output_tokens or 0)),
        "prompt_tokens": max(0, int(prompt_tokens or 0)),
        "completion_tokens": max(0, int(completion_tokens or 0)),
        "total_tokens": max(0, int(total_tokens or 0)),
        "cached_prompt_tokens": max(0, int(cached_prompt_tokens or 0)),
        "reasoning_tokens": max(0, int(reasoning_tokens or 0)),
        "thinking_disabled": bool(thinking_disabled),
        "timing": sanitize_for_trace(dict(timing or {})),
    }
    append_unified_event(
        sanitize_for_trace(event),
        run_id=str(context.get("run_id") or ""),
        output_dir=str(context.get("output_dir") or "") or None,
    )
    try:
        from agent.console_trace import record_llm_timing

        record_llm_timing(
            run_id=str(context.get("run_id") or ""),
            task_id=str(context.get("task_id") or ""),
            worker_id=str(context.get("worker_id") or ""),
            agent_id=str(context.get("agent_id") or ""),
            event_id=event_id,
            stage=str(stage or ""),
            operation=str(operation or ""),
            provider=str(provider or ""),
            model=str(model or ""),
            success=bool(success),
            duration_ms=max(0, int(duration_ms)),
            prompt_tokens=max(0, int(prompt_tokens or 0)),
            completion_tokens=max(0, int(completion_tokens or 0)),
            total_tokens=max(0, int(total_tokens or 0)),
            cached_prompt_tokens=max(0, int(cached_prompt_tokens or 0)),
            reasoning_tokens=max(0, int(reasoning_tokens or 0)),
            thinking_disabled=bool(thinking_disabled),
            timing=dict(timing or {}),
            error_type=str(error_type or ""),
        )
    except Exception:
        pass
    return event_id


def record_schema_result(event_id: str, valid: bool) -> None:
    """Append an immutable parser outcome for an earlier ``LLM_CALL`` event."""
    context = dict(_CONTEXT.get() or {})
    path = _event_path(context)
    if not path or not event_id:
        return
    update = {
        "event_type": "LLM_CALL_SCHEMA",
        "event_id": str(event_id),
        "response_schema_valid": bool(valid),
        "recorded_at": _utc_now(),
    }
    append_unified_event(
        update,
        run_id=str(context.get("run_id") or ""),
        output_dir=str(context.get("output_dir") or "") or None,
    )


def load_llm_events(output_dir: str | Path, run_id: str) -> list[dict[str, Any]]:
    """Read a persisted audit stream and merge its schema-result records."""
    context = {"output_dir": str(output_dir), "run_id": str(run_id)}
    path = _event_path(context)
    if path is None or not path.exists():
        return []
    events: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if value.get("event_type") == "LLM_CALL" and value.get("event_id"):
            event_id = str(value["event_id"])
            events[event_id] = value
            order.append(event_id)
        elif value.get("event_type") == "LLM_CALL_SCHEMA" and value.get("event_id") in events:
            events[str(value["event_id"])]["response_schema_valid"] = value.get("response_schema_valid")
    return [sanitize_for_trace(events[event_id]) for event_id in order]
