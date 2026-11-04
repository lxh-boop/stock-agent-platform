from __future__ import annotations

from agent_graph.services.strategy_context_service import StrategyContextService
from database.repositories import AgentRepository
from portfolio.paper_account import create_default_account
from portfolio.paper_position import create_position
from portfolio.storage import PortfolioStorage
from strategy_workflow_test_utils import database_path, save_draft


def test_strategy_context_loads_scoped_facts_and_proposal(tmp_path) -> None:
    db_path = database_path(tmp_path)
    output_dir = tmp_path / "outputs"
    storage = PortfolioStorage(
        db_path,
        output_dir=output_dir / "portfolio" / "u1",
    )
    storage.save_account(create_default_account("u1", initial_cash=100000))
    storage.save_positions(
        [
            create_position(
                "u1",
                "000001",
                quantity=100,
                cost_price=10,
                current_price=11,
                total_assets=100000,
            )
        ]
    )
    repo = AgentRepository(db_path)
    repo.upsert_conversation(
        {"conversation_id": "conv_1", "user_id": "u1", "title": "策略讨论"}
    )
    repo.upsert_message(
        {
            "message_id": "msg_1",
            "conversation_id": "conv_1",
            "user_id": "u1",
            "role": "user",
            "content": "以后稳健一点",
        }
    )
    draft = save_draft(tmp_path)

    context = StrategyContextService(
        db_path=db_path,
        output_dir=output_dir,
    ).load(
        user_id="u1",
        account_id="paper_u1",
        conversation_id="conv_1",
    )

    assert context.current_account["account_id"] == "paper_u1"
    assert context.current_positions[0]["stock_code"] == "000001"
    assert context.current_strategy["runtime_mode"] == "hierarchical_top10"
    assert context.current_strategy["registry_currently_consumed_by_pipeline"] is True
    assert context.strategy_capabilities["config_schema"]
    assert context.related_conversation[0]["content"] == "以后稳健一点"
    assert context.active_proposal["proposal_id"] == draft.data["proposal"]["proposal_id"]
    assert context.proposal_version_history[0]["version"] == 1
