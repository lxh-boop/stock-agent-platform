"""Slim run-trace surface for the LangGraph engine.

Replaces the legacy 1600-line ``agent/console_trace.py``. Two outputs, both
best-effort (tracing must never break a run):

- console + structured stdout lines for live debugging
- ``outputs/agent_runs/<run_id>/flow.md`` append-only trajectory, which the
  operations flow and ``/runs/{run_id}`` diagnostics continue to use
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_log_directory(output_dir: str | Path = "outputs", run_id: str = "") -> Path:
    root = Path(output_dir) / "agent_runs"
    if run_id:
        return root / str(run_id)
    return root


def _flow_path(output_dir: str | Path, run_id: str) -> Path | None:
    if not str(run_id or "").strip():
        return None
    return run_log_directory(output_dir, run_id) / "flow.md"


def _append_flow(output_dir: str | Path, run_id: str, line: str) -> None:
    path = _flow_path(output_dir, run_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:  # noqa: BLE001 - tracing must never break the run
        pass


def trace_event(
    event_name: str,
    payload: dict[str, Any] | None = None,
    *,
    run_id: str = "",
    output_dir: str | Path = "outputs",
    **_legacy: Any,
) -> None:
    """Record one structured event to console and the run's flow.md.

    Legacy keyword arguments (task_id, user_id, stage, ...) are accepted and
    ignored so old call sites keep working against the slim tracer.
    """

    record = {"at": _now(), "event": str(event_name or "event"), "payload": dict(payload or {})}
    try:
        print(json.dumps({"kind": "trace_event", **record}, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        pass
    try:
        encoded = json.dumps(record, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        encoded = json.dumps({"at": record["at"], "event": record["event"]}, ensure_ascii=False)
    _append_flow(output_dir, str(run_id or ""), f"- `{record['at']}` **{record['event']}** {encoded[:2000]}")


def flow_event(
    event_name: str,
    payload: dict[str, Any] | None = None,
    *,
    run_id: str = "",
    output_dir: str | Path = "outputs",
    stage: str = "",
    **_legacy: Any,
) -> None:
    """Stage-level marker in flow.md (legacy kwargs accepted and ignored)."""

    prefix = f"## {stage} — " if stage else "- "
    record = {"at": _now(), "event": str(event_name or "event"), "payload": dict(payload or {})}
    try:
        encoded = json.dumps(record, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        encoded = ""
    _append_flow(output_dir, str(run_id or ""), f"{prefix}**{record['event']}** {encoded[:2000]}")


def trace_exception(exc: BaseException, *, run_id: str = "", output_dir: str | Path = "outputs") -> None:
    trace_event(
        "exception",
        {"type": type(exc).__name__, "message": str(exc)[:800]},
        run_id=run_id,
        output_dir=output_dir,
    )


def runtime_debug_event(event_name: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Legacy hook kept as a no-op sink; debug dumps died with the old runtime."""

    return None


__all__ = ["trace_event", "trace_exception", "flow_event", "runtime_debug_event", "run_log_directory"]
