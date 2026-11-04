from __future__ import annotations

from typing import Any

from agent_graph.domain_tools.python_sandbox_tool import run_python_sandbox_analysis
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult


class PythonSandboxService:
    """Read-only service wrapper around the restricted analysis sandbox."""

    def run_analysis(
        self,
        code: str,
        *,
        snapshot: dict[str, Any] | None = None,
        snapshot_id: str = "",
        timeout_seconds: float = 995.0,
        max_output_chars: int = 4000,
    ) -> ToolResult:
        result = run_python_sandbox_analysis(
            code,
            snapshot=snapshot or {},
            snapshot_id=snapshot_id,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
        data = dict(result.data or {})
        data.update(
            {
                "read_only": True,
                "mutation_performed": False,
                "business_state_write_allowed": False,
            }
        )
        return ToolResult(
            success=bool(result.success),
            message=str(result.message or ""),
            data=data,
            warnings=list(result.warnings or []),
            errors=list(result.errors or []),
            permission=ToolPermission.READ,
            tool_name="sandbox.python_analysis",
            disclaimer=result.disclaimer,
            status=result.status,
            requires_confirmation=False,
        )


python_sandbox_service = PythonSandboxService()
