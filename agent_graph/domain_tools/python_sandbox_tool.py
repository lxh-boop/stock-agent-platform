from __future__ import annotations

from typing import Any

from agent_graph.domain_tools.sandbox import run_python_analysis
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult


def run_python_sandbox_analysis(
    code: str,
    snapshot: dict[str, Any] | None = None,
    snapshot_id: str = "",
    timeout_seconds: float = 995.0,
    max_output_chars: int = 4000,
) -> ToolResult:
    result = run_python_analysis(
        code,
        snapshot=snapshot or {},
        snapshot_id=snapshot_id,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )
    return ToolResult(
        success=bool(result.get("success")),
        message=str(result.get("status") or ""),
        data=result,
        warnings=list(result.get("warnings") or []),
        errors=[str(result.get("error_type"))] if result.get("error_type") else [],
        permission=ToolPermission.READ,
        tool_name="python_sandbox_analysis",
    )
