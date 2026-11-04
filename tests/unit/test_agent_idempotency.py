from __future__ import annotations

from agent_graph.domain_tools.paper_trade_execute_tool import execute_confirmed_paper_trade_plan
from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper
from agent_control_center_utils import write_agent_fixture


def test_agent_confirmed_plan_is_idempotent(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, price=10.0)
    preview = preview_add_stock_to_paper("u1", "600519", output_dir=output_dir, db_path=db_path)
    first = execute_confirmed_paper_trade_plan(
        "u1",
        preview.data["plan_id"],
        preview.data["confirmation_token"],
        output_dir=output_dir,
        db_path=db_path,
    )
    assert first.success
    second = execute_confirmed_paper_trade_plan(
        "u1",
        preview.data["plan_id"],
        preview.data["confirmation_token"],
        output_dir=output_dir,
        db_path=db_path,
    )
    assert not second.success
    assert "already_executed" in second.errors
