"""Offline unit tests for the deterministic write path (agent_graph.writes).

Covers action normalization, intent mapping, gateway failure contracts and
approval-decision parsing. Proposal state-machine execution needs PostgreSQL
and is exercised by the staging E2E instead.
"""

from __future__ import annotations

import pytest

from agent_graph.approval import decision_from_resume
from agent_graph.writes import PLAN_INTENT_TO_TOOL, execute_confirmed_plan_v2, execute_control_action
from agent_graph.writes.gateway import _failure


def test_plan_intent_to_tool_covers_all_commit_intents() -> None:
    expected = {
        "capital_change", "paper_backfill", "disable_strategy",
        "execute_add_stock", "execute_adjust_position", "execute_portfolio_rebalance",
        "register_strategy", "enable_strategy", "apply_strategy_implementation",
        "activate_strategy_binding", "rollback_strategy_binding",
        "execute_strategy_position_change",
    }
    assert set(PLAN_INTENT_TO_TOOL) == expected
    assert all(
        name.endswith("commit") or name in {"approval.confirm_plan", "portfolio.commit_paper_trade"}
        for name in PLAN_INTENT_TO_TOOL.values()
    )


def test_execute_control_action_normalizes_actions(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("STOCK_POSTGRES_PASSWORD", "x")
    with pytest.raises(ValueError, match="unsupported_write_action"):
        execute_control_action(action="bogus", output_dir=tmp_path)


def test_gateway_unknown_plan_returns_plan_not_found(tmp_path) -> None:
    result = execute_confirmed_plan_v2(
        plan_id="agent_plan_missing",
        confirmation_token="token",
        user_id="tester",
        output_dir=tmp_path,
    )
    assert result.success is False
    assert result.error_type == "plan_not_found"


def test_gateway_unsupported_intent(tmp_path, monkeypatch) -> None:
    from agent_graph.session.pending_action_store import save_pending_plan

    monkeypatch.setattr("agent_graph.writes.gateway.get_pending_plan", lambda *a, **k: {"plan_id": "p1", "intent": "unknown_intent"})
    result = execute_confirmed_plan_v2("p1", "token", "tester", output_dir=tmp_path)
    assert result.error_type == "unsupported_plan_intent"


def test_failure_result_shape() -> None:
    result = _failure("some.tool", "boom_type", "boom", data={"k": 1})
    assert result.success is False
    assert result.error_type == "boom_type"
    assert result.data == {"k": 1}


def test_decision_from_resume_values() -> None:
    assert decision_from_resume({"decision": "approved"}) == "approved"
    assert decision_from_resume({"decision": "rejected", "reason": "no"}) == "rejected"
    assert decision_from_resume("approved") == "invalid"
    assert decision_from_resume(None) == "invalid"
