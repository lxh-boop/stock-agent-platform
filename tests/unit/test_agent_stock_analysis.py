from __future__ import annotations

from agent_graph.domain_tools.stock_analysis_tool import analyze_stock
from agent_control_center_utils import write_agent_fixture


def test_agent_stock_analysis_uses_ranking_and_recommendation(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path)
    result = analyze_stock("u1", "600519", output_dir=output_dir, db_path=db_path, include_rag=False)
    assert result.success
    assert result.data["stock_code"] == "600519"
    assert "final_action" not in result.data
    assert result.data["position_adjustment_ratio"] == 0.8
    assert result.data["current_price"] == 10.0
    assert "suitable" in result.data["suitability_for_user"]
