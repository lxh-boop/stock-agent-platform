"""Approval-commit gateway for pending confirmation plans.

Port of the legacy ``agent/write_gateway.py``: maps a confirmed plan's intent
to its commit tool and executes it with the confirmation token. Message-bus
publishing was removed with the legacy communication stack; results are traced
instead.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_graph.domain_tools.dispatch import AGENT_MAIN, UnifiedToolResult, execute_tool
from agent_graph.session.pending_action_store import get_pending_plan
from agent_graph.trace import trace_event

PLAN_INTENT_TO_TOOL = {
    "capital_change": "capital.change.commit",
    "paper_backfill": "backfill.commit",
    "disable_strategy": "strategy.disable.commit",
    "execute_add_stock": "portfolio.commit_paper_trade",
    "execute_adjust_position": "portfolio.commit_paper_trade",
    "execute_portfolio_rebalance": "portfolio.commit_paper_trade",
    "register_strategy": "approval.confirm_plan",
    "enable_strategy": "approval.confirm_plan",
    "apply_strategy_implementation": "strategy.apply.commit",
    "activate_strategy_binding": "strategy.binding.commit",
    "rollback_strategy_binding": "strategy.binding.commit",
    "execute_strategy_position_change": "strategy.position.commit",
}


def _failure(tool_name: str, error_type: str, message: str, *, data: dict[str, Any] | None = None) -> UnifiedToolResult:
    from datetime import datetime

    started = datetime.now().isoformat(timespec="seconds")
    return UnifiedToolResult(
        success=False,
        tool_name=tool_name,
        message=message,
        data=dict(data or {}),
        errors=[error_type],
        error_type=error_type,
        error_message=message,
        started_at=started,
        finished_at=started,
        duration_ms=0.0,
    )


def execute_confirmed_plan_v2(
    plan_id: str,
    confirmation_token: str,
    user_id: str,
    conversation_id: str = "",
    run_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> UnifiedToolResult:
    started = time.perf_counter()
    plan_id = str(plan_id or "")
    user_id = str(user_id or "default")

    def _trace(result: UnifiedToolResult, intent: str, selected_tool: str) -> None:
        trace_event(
            "approval.result",
            {
                "plan_id": plan_id,
                "intent": intent,
                "selected_tool": selected_tool,
                "success": bool(result.success),
                "status": "success" if result.success else "failed",
                "message": str(result.message or "")[:500],
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            },
            run_id=str(run_id or ""),
            output_dir=output_dir,
        )

    plan = get_pending_plan(user_id, plan_id, output_dir)
    if not plan:
        result = _failure(
            "approval.confirm_plan",
            "plan_not_found",
            "Pending confirmation plan was not found.",
            data={"plan_id": plan_id},
        )
        _trace(result, "", "approval.confirm_plan")
        return result
    intent = str(plan.get("intent") or "")
    tool_name = PLAN_INTENT_TO_TOOL.get(intent)
    if not tool_name:
        result = _failure(
            "approval.confirm_plan",
            "unsupported_plan_intent",
            "Unsupported confirmation plan intent.",
            data={"plan_id": plan_id, "intent": intent},
        )
        _trace(result, intent, "approval.confirm_plan")
        return result
    result = execute_tool(
        tool_name,
        {"plan_id": plan_id, "confirmation_token": confirmation_token, "user_id": user_id},
        context={
            "user_id": user_id,
            "conversation_id": conversation_id,
            "session_id": conversation_id,
            "run_id": run_id,
            "db_path": db_path,
            "output_dir": output_dir,
            "plan_intent": intent,
            "plan_id": plan_id,
        },
        agent_type=AGENT_MAIN,
        approval_granted=True,
    )
    metadata = dict(result.metadata or {})
    metadata.setdefault(
        "write_gateway",
        {
            "plan_id": plan_id,
            "intent": intent,
            "selected_tool": tool_name,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        },
    )
    wrapped = replace(result, metadata=metadata)
    _trace(wrapped, intent, tool_name)
    return wrapped


__all__ = ["PLAN_INTENT_TO_TOOL", "execute_confirmed_plan_v2"]
