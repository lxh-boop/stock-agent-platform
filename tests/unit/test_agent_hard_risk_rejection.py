from __future__ import annotations

from agent_graph.domain_tools.position_recommendation_tool import recommend_position_weight
from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper
from agent_control_center_utils import write_agent_fixture


def test_agent_hard_risk_rejects_new_buy(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, final_action="exclude", final_score=0.80)
    recommendation = recommend_position_weight("u1", "600519", output_dir=output_dir, db_path=db_path)
    assert recommendation.success
    assert recommendation.data["recommended_weight"] == 0
    preview = preview_add_stock_to_paper("u1", "600519", output_dir=output_dir, db_path=db_path)
    assert not preview.success
    assert "invalid_price_or_quantity" in preview.errors
