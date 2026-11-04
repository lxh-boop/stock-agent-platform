from __future__ import annotations

from agent_graph.session.confirmation_manager import create_confirmation_plan, validate_confirmation
from agent_graph.session.pending_action_store import update_pending_plan
from database.repositories.agent_repository import AgentRepository


def test_confirmation_plan_has_hash_and_persists_action_proposal(tmp_path) -> None:
    output_dir = tmp_path / "outputs"
    db_path = tmp_path / "agent_quant.db"
    plan = create_confirmation_plan(
        "u1",
        "capital_change",
        {
            "operation_type": "cash_flow",
            "before": {"cash": 1000},
            "proposed_changes": [{"flow_type": "deposit", "amount": 100}],
            "after": {"cash": 1100},
            "validation_results": {"amount_positive": True},
        },
        output_dir=output_dir,
        db_path=db_path,
    )

    assert plan["plan_hash"]
    assert plan["snapshot_id"].startswith("snapshot_")
    assert plan["business_state_version"]

    proposal = AgentRepository(db_path).get_action_proposal(plan["plan_id"])
    assert proposal is not None
    assert proposal["plan_hash"] == plan["plan_hash"]
    assert proposal["before_state_summary_json"] == {"cash": 1000}
    assert proposal["proposed_changes_json"] == [{"flow_type": "deposit", "amount": 100}]
    assert "confirmation_token" not in proposal


def test_confirmation_token_is_single_use_after_validation(tmp_path) -> None:
    output_dir = tmp_path / "outputs"
    db_path = tmp_path / "agent_quant.db"
    plan = create_confirmation_plan(
        "u1",
        "capital_change",
        {"operation_type": "cash_flow", "amount": 100},
        output_dir=output_dir,
        db_path=db_path,
    )

    ok, status, _ = validate_confirmation(
        "u1",
        plan["plan_id"],
        plan["confirmation_token"],
        output_dir=output_dir,
        db_path=db_path,
    )
    assert ok is True
    assert status == "confirmed"

    ok, status, _ = validate_confirmation(
        "u1",
        plan["plan_id"],
        plan["confirmation_token"],
        output_dir=output_dir,
        db_path=db_path,
    )
    assert ok is False
    assert status == "confirmation_already_used"
    repo = AgentRepository(db_path)
    assert repo.get_action_proposal(plan["plan_id"])["status"] == "confirmed"
    approvals = repo.store.list("action_approvals", {"plan_id": plan["plan_id"]})
    assert {row["status"] for row in approvals} == {"confirmed", "confirmation_already_used"}


def test_tampered_confirmation_plan_is_rejected(tmp_path) -> None:
    output_dir = tmp_path / "outputs"
    db_path = tmp_path / "agent_quant.db"
    plan = create_confirmation_plan(
        "u1",
        "capital_change",
        {"operation_type": "cash_flow", "amount": 100},
        output_dir=output_dir,
        db_path=db_path,
    )

    update_pending_plan("u1", plan["plan_id"], {"amount": 999999}, output_dir=output_dir)
    ok, status, _ = validate_confirmation(
        "u1",
        plan["plan_id"],
        plan["confirmation_token"],
        output_dir=output_dir,
        db_path=db_path,
    )

    assert ok is False
    assert status == "plan_hash_mismatch"
    assert AgentRepository(db_path).get_action_proposal(plan["plan_id"])["status"] == "invalid"
