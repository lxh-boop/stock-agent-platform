from __future__ import annotations

from typing import Any

from agent_graph.runtime.health import collect_runtime_health_summary
from evaluation.system_monitor import (
    build_system_monitor_snapshot,
    collect_and_store_system_monitor_snapshot,
    list_system_monitor_alerts,
    list_system_monitor_history,
)


__all__ = [
    "SystemMonitorApplicationService",
    "build_agent_runtime_health_summary",
    "build_system_monitor_snapshot",
    "collect_and_store_system_monitor_snapshot",
    "collect_runtime_health_summary",
    "list_system_monitor_alerts",
    "list_system_monitor_history",
]


def build_agent_runtime_health_summary(db_path: str | None = None) -> dict[str, Any]:
    return collect_runtime_health_summary(db_path)


system_monitor_service = object()
