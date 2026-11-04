from __future__ import annotations

from agent_graph.domain_tools.paper_trade_execute_tool import execute_confirmed_paper_trade_plan
from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper
from portfolio.storage import PortfolioStorage
from agent_control_center_utils import write_agent_fixture


def test_agent_write_requires_valid_confirmation(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, price=10.0)
    preview = preview_add_stock_to_paper("u1", "600519", output_dir=output_dir, db_path=db_path)
    result = execute_confirmed_paper_trade_plan("u1", preview.data["plan_id"], "bad-token", output_dir=output_dir, db_path=db_path)
    assert not result.success
    assert "invalid_confirmation_token" in result.errors
    storage = PortfolioStorage(db_path, output_dir=output_dir / "portfolio" / "u1")
    assert storage.load_orders("u1") == []
