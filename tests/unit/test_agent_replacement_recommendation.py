from __future__ import annotations

from agent_graph.domain_tools.replacement_recommendation_tool import recommend_replacements
from agent_control_center_utils import write_agent_fixture


def test_agent_replacement_recommendation_ranks_existing_positions(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, with_position=True)
    result = recommend_replacements("u1", "600519", 0.05, output_dir=output_dir, db_path=db_path)
    assert result.success
    candidates = result.data["replacement_candidates"]
    assert candidates
    assert candidates[0]["stock_code"] == "000001"
