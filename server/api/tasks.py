from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import OUTPUT_DIR
from server.api.contracts import OperationResponse
from server.api.dispatch import llm_settings_registry
from server.api.serialization import decode_transport, encode_transport
from server.task_runtime.manager import TaskManager
from server.task_runtime.store import TERMINAL_STATUSES

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class LazyTaskManager:
    """Create the PostgreSQL-backed TaskManager only when runtime work needs it.

    Importing API modules, building OpenAPI, or inspecting routes must stay free of
    database side effects. The real API lifespan explicitly initializes the
    manager when startup recovery is enabled; task endpoints initialize it on
    first use otherwise.
    """

    def __init__(self) -> None:
        self._instance: TaskManager | None = None
        self._lock = threading.RLock()

    @property
    def initialized(self) -> bool:
        return self._instance is not None

    def _get(self) -> TaskManager:
        instance = self._instance
        if instance is not None:
            return instance
        with self._lock:
            if self._instance is None:
                self._instance = TaskManager()
            return self._instance

    @property
    def store(self):
        return self._get().store

    def recover_on_api_startup(self) -> list[str]:
        return self._get().recover_on_api_startup()

    def submit(self, **kwargs):
        return self._get().submit(**kwargs)

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self._get().cancel(task_id)


task_manager = LazyTaskManager()


class TaskSubmitRequest(BaseModel):
    task_type: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    owner_id: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 99600
    max_retries: int = 0


TASK_REQUEST_SENSITIVE_KEYS = {
    "api_key",
    "credential",
    "confirmation_token",
    "db_path",
    "database_path",
    "output_dir",
    "password",
    "secret",
    "token",
    "base_url",
}


def _safe_task_request_value(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in TASK_REQUEST_SENSITIVE_KEYS or any(
                marker in lowered
                for marker in ("api_key", "password", "credential", "confirmation_token")
            ):
                continue
            output[key] = _safe_task_request_value(item)
        return output
    if isinstance(value, list):
        return [_safe_task_request_value(item) for item in value]
    return value


def _public_task(task: Any) -> Any:
    if not isinstance(task, dict) or "task_id" not in task:
        return task
    output = dict(task)
    request = output.get("request")
    if isinstance(request, dict):
        output["request"] = {
            "args": list(request.get("args") or []),
            "kwargs": _safe_task_request_value(dict(request.get("kwargs") or {})),
        }
    metadata = output.get("metadata")
    if isinstance(metadata, dict):
        output["metadata"] = _safe_task_request_value(metadata)
    return output


def _public_task_payload(data: Any) -> Any:
    if isinstance(data, list):
        return [_public_task(item) for item in data]
    return _public_task(data)


def _success(data: Any) -> OperationResponse:
    return OperationResponse(
        success=True,
        data=encode_transport(_public_task_payload(data)),
        request_id=uuid.uuid4().hex,
    )


def _failure(exc: Exception) -> OperationResponse:
    return OperationResponse(
        success=False,
        error={"code": type(exc).__name__, "message": str(exc), "details": {}},
        request_id=uuid.uuid4().hex,
    )


@router.post("", response_model=OperationResponse)
def submit_task(request: TaskSubmitRequest) -> OperationResponse:
    args = list(decode_transport(request.args) or [])
    kwargs = dict(decode_transport(request.kwargs) or {})
    secrets: dict[str, str] = {}
    if request.task_type == "agent.run":
        # Browser clients submit only business identifiers. Server-owned paths and
        # identity fields are injected here so they cannot drift across surfaces.
        kwargs["user_id"] = str(request.owner_id or kwargs.get("user_id") or "default")
        kwargs["session_id"] = str(request.session_id or kwargs.get("session_id") or "")
        kwargs.setdefault("output_dir", str(OUTPUT_DIR))
        kwargs.setdefault("top_k", 10)
        # New background Agent tasks always bind the current persisted model
        # configuration. Browser-side tokens or stale descriptors cannot switch
        # the task back to a previously selected local model.
        kwargs.pop("llm_settings", None)
        settings = llm_settings_registry.resolve_current()
        if settings is not None:
            kwargs["llm_settings_descriptor"] = {
                "profile_id": str(settings.profile_id),
                "config_hash": str(settings.config_hash),
                "mode": str(settings.mode),
                "provider": str(settings.provider),
                "base_url": str(settings.base_url),
                "model": str(settings.model),
                "disable_thinking": bool(settings.disable_thinking),
                "request_timeout_seconds": int(settings.request_timeout_seconds),
                "max_retries": int(settings.max_retries),
                "context_window": int(settings.profile.context_window),
                "supports_json_schema": bool(settings.profile.supports_json_schema),
                "supports_tools": bool(settings.profile.supports_tools),
                "endpoint_scope": str(settings.endpoint_scope),
            }
            secrets["llm_credential"] = str(getattr(settings, "credential", "") or "")
    try:
        task = task_manager.submit(
            task_type=request.task_type,
            args=args,
            kwargs=kwargs,
            owner_id=request.owner_id,
            session_id=request.session_id,
            metadata=decode_transport(request.metadata) or {},
            timeout_seconds=request.timeout_seconds,
            max_retries=request.max_retries,
            secrets=secrets,
        )
        return _success(task)
    except Exception as exc:
        return _failure(exc)


@router.get("", response_model=OperationResponse)
def list_tasks(
    owner_id: str = "",
    session_id: str = "",
    task_type: str = "",
    active_only: bool = False,
    unacknowledged_only: bool = False,
    limit: int = 20,
) -> OperationResponse:
    try:
        return _success(task_manager.store.list(
            owner_id=owner_id,
            session_id=session_id,
            task_type=task_type,
            active_only=active_only,
            unacknowledged_only=unacknowledged_only,
            limit=limit,
        ))
    except Exception as exc:
        return _failure(exc)


@router.get("/{task_id}", response_model=OperationResponse)
def get_task(task_id: str) -> OperationResponse:
    try:
        return _success(task_manager.store.get(task_id))
    except Exception as exc:
        return _failure(exc)


@router.post("/{task_id}/cancel", response_model=OperationResponse)
def cancel_task(task_id: str) -> OperationResponse:
    try:
        return _success(task_manager.cancel(task_id))
    except Exception as exc:
        return _failure(exc)


@router.post("/{task_id}/acknowledge", response_model=OperationResponse)
def acknowledge_task(task_id: str) -> OperationResponse:
    try:
        return _success(task_manager.store.acknowledge(task_id))
    except Exception as exc:
        return _failure(exc)


@router.get("/{task_id}/events")
def task_events(
    task_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    start_after = max(after, int(last_event_id or 0))
    task_manager.store.get(task_id)

    def stream():
        sequence = start_after
        idle = 0
        while True:
            events = task_manager.store.events(task_id, after_sequence=sequence, limit=200)
            for event in events:
                sequence = int(event["sequence"])
                payload = json.dumps(encode_transport(event), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {sequence}\nevent: task-event\ndata: {payload}\n\n"
            task = task_manager.store.get(task_id)
            if task["status"] in TERMINAL_STATUSES and not events:
                payload = json.dumps(encode_transport({"task": _public_task(task)}), ensure_ascii=False, separators=(",", ":"))
                yield f"event: task-complete\ndata: {payload}\n\n"
                break
            idle += 1
            if idle % 10 == 0:
                yield ": heartbeat\n\n"
            time.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
