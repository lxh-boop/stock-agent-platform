from agent_graph.domain_tools.manual_position_operation_tool import preview_manual_position_operation
from portfolio.paper_account import account_from_dict
from portfolio.storage import PortfolioStorage


def test_inconsistent_snapshot_blocks_recommendation(tmp_path) -> None:
    storage = PortfolioStorage(output_dir=tmp_path / "portfolio" / "u1", use_database=False)
    storage.save_account(account_from_dict({"account_id": "other_account", "user_id": "u1", "cash": 100000}))

    result = preview_manual_position_operation("u1", stock_code="000001", requested_weight=0.1, output_dir=tmp_path)

    assert not result.success
    assert result.data["error_code"] == "portfolio_snapshot_inconsistent"
    assert result.data["safe_to_write"] is False
