from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.session.confirmation_manager import create_confirmation_plan, mark_plan_executed, validate_confirmation
from agent_graph.domain_tools._common import now_text, safe_float
from agent_graph.domain_tools.audit_tool import write_agent_action_log, write_agent_confirmation_log
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from portfolio.cash_flow import add_cash_flow, parse_date_text


def preview_capital_change(
    user_id: str,
    flow_type: str,
    amount: float,
    effective_date: str,
    reason: str = "",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    from agent_graph.services.write_operation_service import write_operation_service

    return write_operation_service.create_capital_change_proposal(
        user_id,
        flow_type,
        amount,
        effective_date,
        reason=reason,
        output_dir=output_dir,
        db_path=db_path,
        session_id=session_id,
    )


def execute_confirmed_capital_plan(
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    from agent_graph.services.write_operation_service import write_operation_service

    return write_operation_service.commit_capital_change(
        user_id,
        plan_id,
        confirmation_token,
        output_dir=output_dir,
        db_path=db_path,
        session_id=session_id,
    )
