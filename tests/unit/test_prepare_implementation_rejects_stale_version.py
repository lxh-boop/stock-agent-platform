from __future__ import annotations

from agent_graph.domain_tools.strategy_workflow_tools import prepare_strategy_implementation
from strategy_workflow_test_utils import database_path, save_draft


def test_prepare_implementation_rejects_stale_version(tmp_path) -> None:
    draft = save_draft(
        tmp_path,
        proposal_json={"config": {"entry_top_k": 8}},
    )
    proposal = draft.data["proposal"]
    result = prepare_strategy_implementation(
        proposal_id=proposal["proposal_id"],
        proposal_version=2,
        user_id="u1",
        account_id="paper_u1",
        conversation_id="conv_1",
        run_id="run_test",
        db_path=database_path(tmp_path),
        runtime_dir=tmp_path / "runtime",
    )

    assert result.success is False
    assert "stale_proposal_version" in result.errors
    assert not (tmp_path / "runtime" / "strategy_drafts").exists()
