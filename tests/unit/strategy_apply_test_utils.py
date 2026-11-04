from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.strategy_apply_service import StrategyApplyService
from agent_graph.domain_tools.strategy_workflow_tools import create_strategy_apply_plan
from strategy_workflow_test_utils import database_path, prepare_proposal


def validated_implementation(
    tmp_path: Path,
    *,
    implementation_type: str = "config",
    proposal_json: dict[str, Any] | None = None,
):
    if proposal_json is None:
        if implementation_type == "code":
            proposal_json = {
                "implementation_type": "code",
                "new_capability_spec": {"name": "dynamic_exposure"},
            }
        else:
            proposal_json = {
                "implementation_type": "config",
                "config": {
                    "entry_top_k": 9,
                    "max_positions": 9,
                    "target_invested_weight": 0.70,
                    "minimum_cash_ratio": 0.15,
                },
            }
    return prepare_proposal(tmp_path, proposal_json)


def apply_plan(tmp_path: Path, **kwargs):
    draft, implementation = validated_implementation(tmp_path, **kwargs)
    plan = create_strategy_apply_plan(
        implementation_id=implementation.data["implementation_id"],
        user_id="u1",
        account_id="paper_u1",
        conversation_id="conv_1",
        run_id="run_apply_test",
        db_path=database_path(tmp_path),
        output_dir=tmp_path / "outputs",
        runtime_dir=tmp_path / "runtime",
        project_root=tmp_path / "formal_project",
    )
    return draft, implementation, plan


def apply_service(tmp_path: Path) -> StrategyApplyService:
    return StrategyApplyService(
        db_path=database_path(tmp_path),
        output_dir=tmp_path / "outputs",
        runtime_dir=tmp_path / "runtime",
        project_root=tmp_path / "formal_project",
    )
