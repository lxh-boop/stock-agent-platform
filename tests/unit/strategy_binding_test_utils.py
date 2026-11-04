from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_graph.services.strategy_binding_service import StrategyBindingService
from strategy_apply_test_utils import apply_plan, apply_service


def register_strategy(
    tmp_path: Path,
    *,
    config: dict[str, Any] | None = None,
):
    proposal_json = {
        "implementation_type": "config",
        "config": config
        or {
            "entry_top_k": 9,
            "max_positions": 9,
            "target_invested_weight": 0.70,
            "minimum_cash_ratio": 0.15,
        },
    }
    _, _, plan = apply_plan(
        tmp_path,
        proposal_json=proposal_json,
    )
    committed = apply_service(tmp_path).commit(
        user_id="u1",
        plan_id=plan.data["plan_id"],
        confirmation_token=plan.data["confirmation_token"],
        conversation_id="conv_1",
    )
    assert committed.success
    return committed.data["strategy_manifest"]


def binding_service(tmp_path: Path) -> StrategyBindingService:
    return StrategyBindingService(
        db_path=tmp_path / "agent_quant.db",
        output_dir=tmp_path / "outputs",
    )


def create_binding_plan(
    tmp_path: Path,
    manifest: dict[str, Any],
    *,
    user_id: str = "u1",
    account_id: str = "paper_u1",
    effective_from: str | None = None,
):
    return binding_service(tmp_path).create_activation_plan(
        user_id=user_id,
        account_id=account_id,
        strategy_id=manifest["strategy_id"],
        strategy_version=manifest["version"],
        effective_from=effective_from
        or datetime.now(UTC).date().isoformat(),
        conversation_id="conv_1",
        run_id="run_binding_test",
    )


def confirm_binding(
    tmp_path: Path,
    plan,
    *,
    user_id: str = "u1",
):
    return binding_service(tmp_path).commit(
        user_id=user_id,
        plan_id=plan.data["plan_id"],
        confirmation_token=plan.data["confirmation_token"],
        conversation_id="conv_1",
    )
