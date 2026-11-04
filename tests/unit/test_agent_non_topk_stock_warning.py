from __future__ import annotations

from agent_graph.domain_tools.stock_analysis_tool import analyze_stock
from agent_control_center_utils import write_agent_fixture


def test_agent_non_topk_stock_warning(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, rank=300)
    result = analyze_stock("u1", "600519", output_dir=output_dir, db_path=db_path, top_k=50, include_rag=False)
    assert result.success
    assert "outside the selected TopK" in result.data["non_topk_warning"]
