from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.strategy_proposal_service import StrategyProposalService
from agent_graph.domain_tools.strategy_workflow_tools import save_strategy_proposal_draft
from agent_graph.domain_tools.strategy_workflow_tools import prepare_strategy_implementation


def database_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_quant.db"


def save_draft(
    tmp_path: Path,
    *,
    user_id: str = "u1",
    account_id: str = "paper_u1",
    conversation_id: str = "conv_1",
    proposal_json: dict[str, Any] | None = None,
    feedback: str = "",
    action: str = "save_proposal",
    proposal_id: str = "",
):
    return save_strategy_proposal_draft(
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
        original_request="以后稳健一点",
        proposal_json=(
            proposal_json
            if proposal_json is not None
            else {
                "config": {
                    "entry_top_k": 10,
                    "target_invested_weight": 0.75,
                }
            }
        ),
        user_feedback=feedback,
        change_summary=feedback or "初始草案",
        conversation_action=action,
        proposal_id=proposal_id,
        source_run_id="run_test",
        db_path=database_path(tmp_path),
    )


def proposal_service(tmp_path: Path) -> StrategyProposalService:
    return StrategyProposalService(database_path(tmp_path))


def prepare_proposal(
    tmp_path: Path,
    proposal_json: dict[str, Any],
    *,
    user_id: str = "u1",
    account_id: str = "paper_u1",
    conversation_id: str = "conv_1",
):
    formal_project = tmp_path / "formal_project"
    for relative in [
        Path("portfolio/hierarchical_top10_allocator.py"),
        Path("portfolio/rebalance_rules.py"),
        Path("strategies/adapters/hierarchical_top10_strategy.py"),
    ]:
        target = formal_project / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(relative.read_bytes())
    draft = save_draft(
        tmp_path,
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
        proposal_json=proposal_json,
    )
    proposal = draft.data["proposal"]
    result = prepare_strategy_implementation(
        proposal_id=proposal["proposal_id"],
        proposal_version=proposal["current_version"],
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
        run_id="run_prepare_test",
        db_path=database_path(tmp_path),
        runtime_dir=tmp_path / "runtime",
        project_root=formal_project,
    )
    return draft, result
