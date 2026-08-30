"""Canonical machine-event contract for the Agent Runtime.

This module is deliberately independent from business planning and execution.
It standardises correlation fields, event domains and error categories while
preserving legacy fields for backwards compatibility.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import uuid4


EVENT_SCHEMA_VERSION = "agent_runtime_event.v1"


class EventDomain(str, Enum):
    RUN = "run"
    REQUEST = "request"
    CONTEXT = "context"
    GRAPH = "graph"
    NEED = "need"
    WORKER = "worker"
    TOOL = "tool"
    REPLAN = "replan"
    RECOVERY = "recovery"
    LLM = "llm"
    REPORT = "report"
    SECURITY = "security"
    SYSTEM = "system"


class ErrorCategory(str, Enum):
    NONE = "none"
    PARAMETER_MISSING = "parameter_missing"
    CONTEXT_MISSING = "context_missing"
    DEPENDENCY_MISSING = "dependency_missing"
    PERMISSION_DENIED = "permission_denied"
    VALIDATION_ERROR = "validation_error"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    TOOL_TIMEOUT = "tool_timeout"
    BUSINESS_EMPTY = "business_empty"
    EXTERNAL_SERVICE_ERROR = "external_service_error"
    LLM_ERROR = "llm_error"
    DAG_ERROR = "dag_error"
    WORKER_EXECUTION_ERROR = "worker_execution_error"
    INTERNAL_ERROR = "internal_error"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_int(*values: Any) -> int | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_float(*values: Any) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def event_domain(event_name: str, event_type: str = "") -> EventDomain:
    name = f"{event_type} {event_name}".upper()
    if "REPLAN" in name:
        return EventDomain.REPLAN
    if "RECOVERY" in name or "CHECKPOINT" in name or "RESUME" in name:
        return EventDomain.RECOVERY
    if "LLM" in name or event_type.upper().startswith("LLM_"):
        return EventDomain.LLM
    if "TOOL" in name:
        return EventDomain.TOOL
    if "WORKER" in name:
        return EventDomain.WORKER
    if "NEED" in name:
        return EventDomain.NEED
    if "GRAPH" in name:
        return EventDomain.GRAPH
    if "CONTEXT" in name or "MEMORY" in name:
        return EventDomain.CONTEXT
    if "REQUEST" in name:
        return EventDomain.REQUEST
    if "REPORT" in name or "PRESENTATION" in name:
        return EventDomain.REPORT
    if "VALIDATION" in name or "PERMISSION" in name or "APPROVAL" in name:
        return EventDomain.SECURITY
    if "RUN" in name or "EXECUTOR" in name:
        return EventDomain.RUN
    return EventDomain.SYSTEM


def classify_error(
    *,
    error_type: str = "",
    error_code: str = "",
    failure_kind: str = "",
    message: str = "",
) -> ErrorCategory:
    text = " ".join(
        part.lower()
        for part in (error_type, error_code, failure_kind, message)
        if str(part or "").strip()
    )
    if not text:
        return ErrorCategory.NONE
    if any(key in text for key in ("missing_parameter", "parameter_missing", "user_input_required", "missing user business parameter")):
        return ErrorCategory.PARAMETER_MISSING
    if any(key in text for key in ("context_missing", "missing_context", "context_insufficient", "need_context")):
        return ErrorCategory.CONTEXT_MISSING
    if any(key in text for key in ("dependency", "upstream_tool_failed", "blocked_by_failed_dependency", "unresolved_task_dependency")):
        return ErrorCategory.DEPENDENCY_MISSING
    if any(key in text for key in ("permission", "unauthorized", "forbidden", "not_allowed")):
        return ErrorCategory.PERMISSION_DENIED
    if any(key in text for key in ("timeout", "timed out")):
        return ErrorCategory.TOOL_TIMEOUT
    if any(key in text for key in ("business_empty", "empty_result", "no_records", "result_empty")):
        return ErrorCategory.BUSINESS_EMPTY
    if any(key in text for key in ("llmjson", "structured_output", "schema", "validation", "contract_violation", "invalid_argument")):
        return ErrorCategory.VALIDATION_ERROR
    if any(key in text for key in ("tool_dag", "dag_cycle", "dag_execution_stalled", "planning_failure")):
        return ErrorCategory.DAG_ERROR
    if any(key in text for key in ("external_service", "provider_error", "connectionerror", "http_error", "network")):
        return ErrorCategory.EXTERNAL_SERVICE_ERROR
    if any(key in text for key in ("llm_error", "model_error", "provider_llm", "completion_error")):
        return ErrorCategory.LLM_ERROR
    if any(key in text for key in ("tool_failure", "tool_execution", "tool_reported_failure")):
        return ErrorCategory.TOOL_EXECUTION_ERROR
    if any(key in text for key in ("worker_execution", "worker_private_tool_planning_failure")):
        return ErrorCategory.WORKER_EXECUTION_ERROR
    return ErrorCategory.INTERNAL_ERROR


def _normalise_level(record: dict[str, Any], payload: dict[str, Any]) -> str:
    explicit = _first_text(record.get("level"), payload.get("level")).upper()
    if explicit in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        return explicit
    success = record.get("success", payload.get("success"))
    status = _first_text(record.get("status"), payload.get("status")).lower()
    if success is False or status in {"failed", "error"}:
        return "ERROR"
    if status in {"partial", "insufficient", "need_context", "blocked", "warning"}:
        return "WARNING"
    return "INFO"


def _correlation_value(record: dict[str, Any], payload: dict[str, Any], metadata: dict[str, Any], *keys: str) -> str:
    for source in (record, payload, metadata):
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def build_event_envelope(
    event: dict[str, Any],
    *,
    run_id: str,
    sequence: int,
    time_value: str,
) -> dict[str, Any]:
    """Return a backwards-compatible canonical runtime event.

    Existing event-specific fields stay at top level. Canonical correlation and
    error fields are added so JSONL can be queried consistently across Flow,
    Runtime Debug, Tool and LLM events.
    """
    record = dict(event or {})
    payload = _mapping(record.get("payload"))
    metadata = _mapping(record.get("metadata"))
    error = _mapping(record.get("error")) or _mapping(payload.get("error"))

    event_type = _first_text(record.get("event_type"), "RUNTIME_EVENT")
    event_name = _first_text(record.get("event_name"), record.get("stage"), event_type).upper()
    worker_task_id = _correlation_value(record, payload, metadata, "worker_task_id", "task_id")
    worker_id = _correlation_value(record, payload, metadata, "worker_id")
    request_id = _correlation_value(record, payload, metadata, "request_id")
    need_id = _correlation_value(record, payload, metadata, "need_id")
    tool_call_id = _correlation_value(record, payload, metadata, "tool_call_id")
    tool_task_id = _correlation_value(record, payload, metadata, "tool_task_id")
    session_id = _correlation_value(record, payload, metadata, "session_id", "conversation_id")
    user_id = _correlation_value(record, payload, metadata, "user_id")
    agent_id = _correlation_value(record, payload, metadata, "agent_id")
    component = _first_text(
        record.get("component"), payload.get("component"), error.get("component"), metadata.get("component"),
        worker_id, agent_id,
    )

    error_type = _first_text(record.get("error_type"), payload.get("error_type"), error.get("type"))
    error_code = _first_text(
        record.get("error_code"), payload.get("error_code"), error.get("code"), error.get("error_id"),
        record.get("failure_kind"), payload.get("failure_kind"), metadata.get("failure_kind"),
    )
    failure_kind = _first_text(record.get("failure_kind"), payload.get("failure_kind"), metadata.get("failure_kind"))
    message = _first_text(
        record.get("message"), payload.get("message"), payload.get("reason"), payload.get("summary"),
        error.get("message"), record.get("error_message"), payload.get("error_message"),
    )
    retryable_value = record.get("retryable")
    if retryable_value is None:
        retryable_value = payload.get("retryable")
    if retryable_value is None:
        retryable_value = error.get("retryable")

    record_id = f"evt_{uuid4().hex}"
    canonical = {
        "schema_version": _first_text(record.get("schema_version"), EVENT_SCHEMA_VERSION),
        "record_id": record_id,
        # event_id is kept for legacy LLM_CALL/LLM_CALL_SCHEMA correlation.
        # record_id is the unique identifier of this physical JSONL record.
        "event_id": _first_text(record.get("event_id"), record_id),
        "event_type": event_type,
        "event_name": event_name,
        "event_domain": event_domain(event_name, event_type).value,
        "sequence": int(sequence),
        "time": time_value,
        "level": _normalise_level(record, payload),
        "trace_id": _first_text(record.get("trace_id"), payload.get("trace_id"), metadata.get("trace_id"), run_id),
        "run_id": str(run_id or ""),
        "session_id": session_id,
        "user_id": user_id,
        "request_id": request_id,
        "component": component,
        "agent_id": agent_id,
        "need_id": need_id,
        "worker_task_id": worker_task_id,
        "worker_id": worker_id,
        "tool_task_id": tool_task_id,
        "tool_call_id": tool_call_id,
        "attempt": _first_int(record.get("attempt"), payload.get("attempt"), metadata.get("attempt")),
        "replan_round": _first_int(record.get("replan_round"), payload.get("replan_round"), payload.get("round"), metadata.get("replan_round")),
        "status": _first_text(
            record.get("status"), payload.get("status"),
            "succeeded" if record.get("success", payload.get("success")) is True else "",
            "failed" if record.get("success", payload.get("success")) is False else "",
        ),
        "success": record.get("success", payload.get("success")),
        "duration_ms": _first_float(record.get("duration_ms"), payload.get("duration_ms")),
        "error_category": classify_error(
            error_type=error_type,
            error_code=error_code,
            failure_kind=failure_kind,
            message=message,
        ).value,
        "error_code": error_code,
        "error_type": error_type,
        "failure_kind": failure_kind,
        "retryable": bool(retryable_value) if retryable_value is not None else None,
        "message": message[:1000] if message else "",
    }

    # Keep old task_id for compatibility while making the meaning explicit.
    if worker_task_id and not record.get("task_id"):
        record["task_id"] = worker_task_id

    # Canonical keys come first; event-specific legacy fields remain queryable.
    for key, value in record.items():
        if key not in canonical:
            canonical[key] = value
    return canonical
