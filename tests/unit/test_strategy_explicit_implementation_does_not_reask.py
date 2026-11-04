from __future__ import annotations

from agent_graph.domain_tools.strategy_workflow_tools import (
    IMPLEMENTATION_CONFIRMATION_QUESTION,
)
from strategy_workflow_test_utils import save_draft


def test_strategy_explicit_implementation_enters_preparation_without_reask(tmp_path) -> None:
    first = save_draft(tmp_path)
    result = save_draft(
        tmp_path,
        proposal_json={},
        feedback="就按第二版实施",
        action="prepare_implementation",
        proposal_id=first.data["proposal"]["proposal_id"],
    )

    assert result.success
    assert result.data["implementation_requested"] is True
    assert result.message != IMPLEMENTATION_CONFIRMATION_QUESTION
    assert result.data["formal_write_created"] is False
