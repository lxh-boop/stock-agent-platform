from __future__ import annotations

from pathlib import Path

from agent_graph.session.confirmation_manager import create_confirmation_plan, mark_plan_executed, validate_confirmation
from agent_graph.domain_tools.audit_tool import write_agent_action_log, write_agent_confirmation_log
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from pipelines.paper_backfill_pipeline import run_paper_trading_backfill


def preview_backfill(
    user_id: str,
    start_date: str,
    end_date: str = "latest",
    initial_cash: float | None = None,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    from agent_graph.services.write_operation_service import write_operation_service

    return write_operation_service.create_backfill_proposal(
        user_id,
        start_date,
        end_date=end_date,
        initial_cash=initial_cash,
        output_dir=output_dir,
        db_path=db_path,
        session_id=session_id,
    )


def execute_confirmed_backfill_plan(
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    from agent_graph.services.write_operation_service import write_operation_service

    return write_operation_service.commit_backfill(
        user_id,
        plan_id,
        confirmation_token,
        output_dir=output_dir,
        db_path=db_path,
        session_id=session_id,
    )
