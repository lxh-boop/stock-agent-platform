from __future__ import annotations

import pytest

from agent_graph.services.strategy_implementation_service import (
    StrategyImplementationService,
)
from strategy_workflow_test_utils import database_path, save_draft


def test_prepare_implementation_requires_locked_proposal(tmp_path) -> None:
    draft = save_draft(
        tmp_path,
        proposal_json={"config": {"entry_top_k": 8}},
    )
    proposal = draft.data["proposal"]
    service = StrategyImplementationService(
        db_path=database_path(tmp_path),
        runtime_dir=tmp_path / "runtime",
    )

    with pytest.raises(
        ValueError,
        match="proposal_must_be_locked_for_implementation",
    ):
        service.prepare_locked(
            proposal_id=proposal["proposal_id"],
            proposal_version=1,
            user_id="u1",
            account_id="paper_u1",
            conversation_id="conv_1",
            run_id="run_test",
        )
