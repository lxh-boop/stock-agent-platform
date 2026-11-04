from __future__ import annotations

from agent_graph.domain_tools.strategy_workflow_tools import prepare_strategy_implementation
from strategy_workflow_test_utils import (
    database_path,
    prepare_proposal,
    save_draft,
)


def test_implementation_repeated_call_is_idempotent_and_cross_user_safe(
    tmp_path,
) -> None:
    draft, first = prepare_proposal(
        tmp_path,
        {
            "implementation_type": "config",
            "config": {"entry_top_k": 9, "max_positions": 9},
        },
    )
    proposal = draft.data["proposal"]
    second = prepare_strategy_implementation(
        proposal_id=proposal["proposal_id"],
        proposal_version=1,
        user_id="u1",
        account_id="paper_u1",
        conversation_id="conv_1",
        run_id="run_second",
        db_path=database_path(tmp_path),
        runtime_dir=tmp_path / "runtime",
    )
    cross_user = prepare_strategy_implementation(
        proposal_id=proposal["proposal_id"],
        proposal_version=1,
        user_id="u2",
        account_id="paper_u2",
        conversation_id="conv_1",
        run_id="run_cross",
        db_path=database_path(tmp_path),
        runtime_dir=tmp_path / "runtime",
    )

    assert second.success
    assert first.data["implementation_id"] == second.data["implementation_id"]
    assert first.data["implementation_hash"] == second.data["implementation_hash"]
    assert first.data["artifact_root"] == second.data["artifact_root"]
    assert cross_user.success is False
    assert "proposal_not_found" in cross_user.errors
