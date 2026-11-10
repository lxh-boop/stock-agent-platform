from __future__ import annotations

import json
import traceback as traceback_module
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from scheduler.schemas import JobStatus, SchedulerStatus, StepStatus, now_text


DEFAULT_JOB_DIR = Path("runtime") / "jobs"
LATEST_STATUS_NAME = "latest_job_status.json"


def job_dir(root: str | Path = ".") -> Path:
    return Path(root) / DEFAULT_JOB_DIR


def latest_status_path(root: str | Path = ".") -> Path:
    return job_dir(root) / LATEST_STATUS_NAME


def history_status_path(status: JobStatus, root: str | Path = ".") -> Path:
    token = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return job_dir(root) / "history" / f"job_{token}_{status.run_id}.json"


def save_job_status(status: JobStatus, root: str | Path = ".") -> Path:
    target = latest_status_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(status.to_dict(), ensure_ascii=False, indent=2, default=str)
    target.write_text(payload, encoding="utf-8")
    if status.finished_at:
        history = history_status_path(status, root)
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(payload, encoding="utf-8")
    return target


def load_latest_job_status(root: str | Path = ".") -> dict[str, Any]:
    path = latest_status_path(root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"overall_status": SchedulerStatus.FAILED, "error_message": f"failed to read latest job status: {exc}"}


def start_step(status: JobStatus, step_name: str) -> StepStatus:
    step = StepStatus(step_name=step_name, status=SchedulerStatus.RUNNING, started_at=now_text())
    status.current_step = step_name
    status.step_status[step_name] = step
    return step


def finish_step(
    status: JobStatus,
    step: StepStatus,
    step_status: str,
    metadata: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: BaseException | None = None,
    retry_count: int = 0,
    started_perf: float | None = None,
) -> StepStatus:
    step.status = step_status
    step.finished_at = now_text()
    if started_perf is not None:
        step.duration_seconds = round(perf_counter() - started_perf, 3)
    step.retry_count = int(retry_count)
    if metadata:
        step.metadata.update(metadata)
    if warnings:
        step.warnings.extend(warnings)
        status.warnings.extend(warnings)
    if error:
        step.error_type = type(error).__name__
        step.error_message = str(error)
        step.traceback = traceback_module.format_exc()
        if step.step_name not in status.failed_steps:
            status.failed_steps.append(step.step_name)
    elif step_status in {SchedulerStatus.SUCCESS, SchedulerStatus.SKIPPED}:
        if step.step_name not in status.completed_steps:
            status.completed_steps.append(step.step_name)
    status.current_step = ""
    status.step_status[step.step_name] = step
    return step


def run_recorded_step(
    status: JobStatus,
    step_name: str,
    func: Callable[[], dict[str, Any] | None],
    root: str | Path = ".",
    allow_failure: bool = False,
) -> dict[str, Any]:
    started = perf_counter()
    step = start_step(status, step_name)
    save_job_status(status, root)
    try:
        result = func() or {}
        step_status = str(result.get("status") or SchedulerStatus.SUCCESS)
        if step_status not in {
            SchedulerStatus.SUCCESS,
            SchedulerStatus.SKIPPED,
            SchedulerStatus.PARTIAL_SUCCESS,
            SchedulerStatus.FAILED,
        }:
            step_status = SchedulerStatus.SUCCESS
        error = RuntimeError(str(result.get("error_message") or "step failed")) if step_status == SchedulerStatus.FAILED else None
        finish_step(
            status,
            step,
            step_status,
            metadata=dict(result.get("metadata") or {}),
            warnings=list(result.get("warnings") or []),
            error=error,
            retry_count=int(result.get("retry_count") or 0),
            started_perf=started,
        )
        save_job_status(status, root)
        if error and not allow_failure:
            # Already recorded above; do not record the same error twice.
            raise error
        return result
    except Exception as exc:
        # A returned failure was already persisted in the try block.
        if step.finished_at:
            raise
        finish_step(status, step, SchedulerStatus.FAILED, error=exc, started_perf=started)
        save_job_status(status, root)
        if not allow_failure:
            raise
        return {"status": SchedulerStatus.FAILED, "error_type": type(exc).__name__, "error_message": str(exc)}
