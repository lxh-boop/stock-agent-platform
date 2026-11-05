"""Approval-commit gateway for pending confirmation plans.

Maps a confirmed plan's intent to its commit adapter and calls it directly
(no tool-registry indirection). Message-bus publishing was removed with the
legacy communication stack; results are traced instead.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from agent_graph.domain_tools.portfolio_proposal_adapters import portfolio_commit_paper_trade_adapter
from agent_graph.domain_tools.write_operation_adapters import (
    approval_confirm_plan_adapter,
    backfill_commit_adapter,
    capital_change_commit_adapter,
    strategy_apply_commit_adapter,
    strategy_binding_commit_adapter,
    strategy_disable_commit_adapter,
    strategy_position_commit_adapter,
)
from agent_graph.session.pending_action_store import get_pending_plan
from agent_graph.toolbox.validation import normalise_raw_result
from agent_graph.trace import trace_event

_PLAN_INTENT_TO_ADAPTER: dict[str, tuple[Callable[[dict[str, Any], dict[str, Any]], Any], str]] = {
    "capital_change": (capital_change_commit_adapter, "capital.change.commit"),
    "paper_backfill": (backfill_commit_adapter, "backfill.commit"),
    "disable_strategy": (strategy_disable_commit_adapter, "strategy.disable.commit"),
    "execute_add_stock": (portfolio_commit_paper_trade_adapter, "portfolio.commit_paper_trade"),
    "execute_adjust_position": (portfolio_commit_paper_trade_adapter, "portfolio.commit_paper_trade"),
    "execute_portfolio_rebalance": (portfolio_commit_paper_trade_adapter, "portfolio.commit_paper_trade"),
    "register_strategy": (approval_confirm_plan_adapter, "approval.confirm_plan"),
    "enable_strategy": (approval_confirm_plan_adapter, "approval.confirm_plan"),
    "apply_strategy_implementation": (strategy_apply_commit_adapter, "strategy.apply.commit"),
    "activate_strategy_binding": (strategy_binding_commit_adapter, "strategy.binding.commit"),
    "rollback_strategy_binding": (strategy_binding_commit_adapter, "strategy.binding.commit"),
    "execute_strategy_position_change": (strategy_position_commit_adapter, "strategy.position.commit"),
}


def plan_intent_to_tool(intent: str) -> str:
    """Canonical tool label for one plan intent (trace/audit display only)."""

    entry = _PLAN_INTENT_TO_ADAPTER.get(str(intent or ""))
    return entry[1] if entry else "approval.confirm_plan"


def _failure(tool_name: str, error_type: str, message: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
    from datetime import datetime

    started = datetime.now().isoformat(timespec="seconds")
    return {
        "success": False,
        "tool_name": tool_name,
        "message": message,
        "data": dict(data or {}),
        "errors": [error_type],
        "error_type": error_type,
        "error_message": message,
        "started_at": started,
        "finished_at": started,
        "duration_ms": 0.0,
    }


def execute_confirmed_plan_v2(
    plan_id: str,
    confirmation_token: str,
    user_id: str,
    conversation_id: str = "",
    run_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> dict[str, Any]:
    started = time.perf_counter()
    plan_id = str(plan_id or "")
    user_id = str(user_id or "default")

    def _trace(result: dict[str, Any], intent: str, selected_tool: str) -> None:
        trace_event(
            "approval.result",
            {
                "plan_id": plan_id,
                "intent": intent,
                "selected_tool": selected_tool,
                "success": bool(result.get("success")),
                "status": "success" if result.get("success") else "failed",
                "message": str(result.get("message") or "")[:500],
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
    entry = _PLAN_INTENT_TO_ADAPTER.get(intent)
    if entry is None:
        result = _failure(
            "approval.confirm_plan",
            "unsupported_plan_intent",
            "Unsupported confirmation plan intent.",
            data={"plan_id": plan_id, "intent": intent},
        )
        _trace(result, intent, "approval.confirm_plan")
        return result
    adapter, tool_label = entry
    try:
        raw = adapter(
            {"plan_id": plan_id, "confirmation_token": confirmation_token, "user_id": user_id},
            {
                "user_id": user_id,
                "conversation_id": conversation_id,
                "session_id": conversation_id,
                "run_id": run_id,
                "db_path": db_path,
                "output_dir": output_dir,
                "plan_intent": intent,
                "plan_id": plan_id,
            },
        )
    except Exception as exc:  # noqa: BLE001 - deterministic failure contract
        result = _failure(tool_label, "tool_execution_failed", str(exc)[:400])
        _trace(result, intent, tool_label)
        return result
    result = normalise_raw_result(raw, requested_name=tool_label, canonical_name=tool_label)
    result.setdefault("tool_name", tool_label)
    result["metadata"] = {
        **dict(result.get("metadata") or {}),
        "write_gateway": {
            "plan_id": plan_id,
            "intent": intent,
            "selected_tool": tool_label,
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        },
    }
    _trace(result, intent, tool_label)
    return result


__all__ = ["execute_confirmed_plan_v2", "plan_intent_to_tool"]
