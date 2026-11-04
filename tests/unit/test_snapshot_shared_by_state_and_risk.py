from agent_graph.services.portfolio_risk_service import portfolio_risk_service
from agent_graph.services.portfolio_service import portfolio_service
from portfolio.paper_account import account_from_dict
from portfolio.paper_position import create_position
from portfolio.storage import PortfolioStorage


def test_snapshot_shared_by_state_and_risk(tmp_path) -> None:
    storage = PortfolioStorage(output_dir=tmp_path / "portfolio" / "u1", use_database=False)
    storage.save_account(account_from_dict({"account_id": "paper_u1", "user_id": "u1", "cash": 100000}))
    storage.save_positions([create_position("u1", "000001", quantity=1000, cost_price=10, current_price=12)])

    state = portfolio_service.get_portfolio_state("u1", output_dir=tmp_path)
    risk = portfolio_risk_service.analyze_current_risk("u1", output_dir=tmp_path, portfolio_state=state)

    assert state["snapshot_id"]
    assert risk["snapshot_id"] == state["snapshot_id"]
    assert risk["calculation_trace"] == state["calculation_trace"]

