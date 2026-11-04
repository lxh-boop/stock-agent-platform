from agent_graph.services.portfolio_service import portfolio_service
from portfolio.paper_account import account_from_dict
from portfolio.paper_position import create_position
from portfolio.storage import PortfolioStorage


def test_portfolio_state_output_is_self_consistent(tmp_path) -> None:
    storage = PortfolioStorage(output_dir=tmp_path / "portfolio" / "u1", use_database=False)
    storage.save_account(account_from_dict({"account_id": "paper_u1", "user_id": "u1", "cash": 100000, "initial_cash": 100000, "position_market_value": 0, "total_assets": 100000, "updated_at": "2026-07-17 09:00:00"}))
    storage.save_positions([create_position("u1", "000001", quantity=1000, cost_price=10, current_price=12, total_assets=100000, updated_at="2026-07-17 09:00:00")])

    state = portfolio_service.get_portfolio_state("u1", output_dir=tmp_path)

    assert state["consistency_status"] == "recomputed_stale_summary"
    assert state["summary"]["position_market_value"] == sum(item["market_value"] for item in state["positions"])
    assert state["summary"]["total_assets"] == state["summary"]["cash"] + state["summary"]["position_market_value"]
    assert state["positions"][0]["position_ratio"] == state["positions"][0]["market_value"] / state["summary"]["total_assets"]
