from __future__ import annotations

from typing import Any

from evaluation.system_monitor import (
    build_system_monitor_snapshot,
    collect_and_store_system_monitor_snapshot,
    collect_runtime_health_summary,
    list_system_monitor_alerts,
    list_system_monitor_history,
)


__all__ = [
    "build_agent_runtime_health_summary",
    "build_system_monitor_snapshot",
    "collect_and_store_system_monitor_snapshot",
    "collect_runtime_health_summary",
    "list_system_monitor_alerts",
    "list_system_monitor_history",
]


def build_agent_runtime_health_summary(db_path: str | None = None, *, user_id: str = "default") -> dict[str, Any]:
    return collect_runtime_health_summary(db_path, user_id=user_id)


system_monitor_service = object()
