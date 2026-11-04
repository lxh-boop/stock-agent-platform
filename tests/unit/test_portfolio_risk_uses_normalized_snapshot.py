from agent_graph.services.portfolio_risk_service import portfolio_risk_service
from portfolio.paper_account import account_from_dict
from portfolio.paper_position import create_position
from portfolio.storage import PortfolioStorage


def test_portfolio_risk_uses_normalized_snapshot(tmp_path) -> None:
    storage = PortfolioStorage(output_dir=tmp_path / "portfolio" / "u1", use_database=False)
    storage.save_account(account_from_dict({"account_id": "paper_u1", "user_id": "u1", "cash": 100000, "initial_cash": 100000, "position_market_value": 0, "total_assets": 100000, "updated_at": "2026-07-17 09:00:00"}))
    storage.save_positions([create_position("u1", "000001", quantity=1000, cost_price=10, current_price=12, total_assets=100000, updated_at="2026-07-17 09:00:00")])

    risk = portfolio_risk_service.analyze_current_risk("u1", output_dir=tmp_path)

    assert risk["status"] == "success"
    assert risk["source"] == "normalized_snapshot"
    assert risk["risk_report"]["total_assets"] == 112000
    assert risk["risk_report"]["cash_ratio"] == 100000 / 112000
    assert risk["risk_report"]["max_single_position"] == 12000 / 112000
