from __future__ import annotations

from agent_graph.domain_tools.position_recommendation_tool import recommend_position_weight
from agent_control_center_utils import write_agent_fixture


def test_agent_position_recommendation_caps_weight_and_lot_size(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, price=10.0)
    result = recommend_position_weight("u1", "600519", output_dir=output_dir, db_path=db_path)
    assert result.success
    assert result.data["recommended_weight"] <= result.data["maximum_allowed_weight"]
    assert result.data["estimated_quantity"] % 100 == 0
    assert result.data["estimated_cost"] > 0
