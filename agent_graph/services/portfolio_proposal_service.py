from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    return {}


def _wrap_result(
    result: ToolResult,
    *,
    tool_name: str,
    permission: str | None = None,
    data_patch: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> ToolResult:
    data = dict(result.data or {})
    data.update(dict(data_patch or {}))
    merged_warnings = list(result.warnings or [])
    merged_warnings.extend(item for item in (warnings or []) if item)
    return ToolResult(
        success=bool(result.success),
        message=str(result.message or ""),
        data=data,
        warnings=merged_warnings,
        errors=list(result.errors or []),
        permission=permission or result.permission,
        tool_name=tool_name,
        disclaimer=result.disclaimer,
        status=result.status,
        requires_confirmation=bool(result.requires_confirmation),
        confirmation_token=result.confirmation_token,
    )


class PortfolioProposalService:
    """Domain service for portfolio advice, proposal previews and paper-trade commits.

    The existing portfolio algorithms remain in the legacy tool modules. This service
    centralizes the Agent-facing contract without changing one-lot, cash allocation,
    revalidation, or idempotency rules.
    """

    

    

    

    

    

    

    def commit_paper_trade(
        self,
        user_id: str,
        plan_id: str,
        confirmation_token: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        from agent_graph.domain_tools.paper_trade_execute_tool import execute_confirmed_paper_trade_plan

        result = execute_confirmed_paper_trade_plan(
            user_id,
            plan_id,
            confirmation_token,
            output_dir=output_dir,
            db_path=db_path,
            session_id=session_id,
        )
        data = dict(result.data or {})
        patch = {
            "revalidation_result": {
                "status": "passed" if result.success else "failed",
                "errors": list(result.errors or []),
            },
            "commit_result": data,
            "audit_record": {
                "plan_id": plan_id,
                "tool_name": "portfolio.commit_paper_trade",
                "legacy_commit_engine": "paper_trade_execute_tool.execute_confirmed_paper_trade_plan",
            },
        }
        return _wrap_result(
            result,
            tool_name="portfolio.commit_paper_trade",
            permission=ToolPermission.WRITE,
            data_patch=patch,
        )

    


portfolio_proposal_service = PortfolioProposalService()
