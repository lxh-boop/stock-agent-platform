from __future__ import annotations

from agent_graph.domain_tools.position_recommendation_tool import recommend_position_weight
from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper
from agent_control_center_utils import write_agent_fixture


def test_agent_lot_size_and_price_validation(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, price=1.0)
    invalid = preview_add_stock_to_paper("u1", "600519", output_dir=output_dir, db_path=db_path)
    assert not invalid.success
    assert "invalid_price_or_quantity" in invalid.errors

    output_dir2, db_path2 = write_agent_fixture(tmp_path / "valid", price=10.0)
    valid = recommend_position_weight("u1", "600519", output_dir=output_dir2, db_path=db_path2)
    assert valid.data["estimated_quantity"] % 100 == 0
