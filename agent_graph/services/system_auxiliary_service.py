from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.domain_tools.report_tool import query_latest_reports
from agent_graph.domain_tools.scheduler_tool import query_scheduler_status


class SystemAuxiliaryService:
    """Read-only/system helpers exposed to the Agent through v2 tools."""

    def scheduler_status(self, *, root: str | Path = ".") -> dict[str, Any]:
        data = query_scheduler_status(root)
        success = str(data.get("status") or "").lower() in {"success", "missing_status"}
        return {
            "success": success,
            "message": "Scheduler status loaded." if success else "Scheduler status unavailable.",
            "data": {
                **dict(data or {}),
                "read_only": True,
                "mutation_performed": False,
            },
            "warnings": [] if data.get("latest_job_status") else ["scheduler_status_missing"],
            "errors": [] if success else ["scheduler_status_failed"],
            "tool_name": "system.scheduler_status",
        }

    def list_latest_reports(self, *, output_dir: str | Path = "outputs") -> dict[str, Any]:
        data = query_latest_reports(output_dir)
        success = str(data.get("status") or "").lower() in {"success", "no_reports"}
        return {
            "success": success,
            "message": "Latest reports listed." if success else "Latest reports unavailable.",
            "data": {
                **dict(data or {}),
                "read_only": True,
                "mutation_performed": False,
            },
            "warnings": [] if data.get("reports") else ["no_reports"],
            "errors": [] if success else ["report_list_failed"],
            "tool_name": "report.list_latest",
        }


system_auxiliary_service = SystemAuxiliaryService()
