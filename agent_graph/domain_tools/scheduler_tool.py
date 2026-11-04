from __future__ import annotations

from pathlib import Path
from typing import Any

from scheduler.job_state import load_latest_job_status


def query_scheduler_status(root: str | Path = ".") -> dict[str, Any]:
    status = load_latest_job_status(root)
    log_dir = Path(root) / "logs" / "scheduler"
    logs = sorted(log_dir.glob("*.log")) if log_dir.exists() else []
    tail = ""
    if logs:
        try:
            lines = logs[-1].read_text(encoding="utf-8", errors="ignore").splitlines()
            tail = "\n".join(lines[-20:])
        except Exception:
            tail = ""
    return {
        "status": "success" if status else "missing_status",
        "latest_job_status": status,
        "latest_log_path": str(logs[-1]) if logs else "",
        "latest_log_tail": tail,
    }
