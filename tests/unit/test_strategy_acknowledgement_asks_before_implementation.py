from __future__ import annotations

from agent_graph.domain_tools.strategy_workflow_tools import (
    IMPLEMENTATION_CONFIRMATION_QUESTION,
)
from database.repositories import AgentRepository
from strategy_workflow_test_utils import database_path, save_draft


def test_strategy_acknowledgement_asks_before_implementation(tmp_path) -> None:
    first = save_draft(tmp_path)
    result = save_draft(
        tmp_path,
        proposal_json={},
        feedback="这样可以",
        action="ask_implementation",
        proposal_id=first.data["proposal"]["proposal_id"],
    )

    assert result.message == IMPLEMENTATION_CONFIRMATION_QUESTION
    assert result.data["implementation_requested"] is False
    assert result.data["proposal"]["status"] == "draft"
    assert AgentRepository(database_path(tmp_path)).store.list(
        "action_proposals",
        {},
    ) == []
