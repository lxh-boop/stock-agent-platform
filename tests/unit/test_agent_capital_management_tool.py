from __future__ import annotations

from agent_graph.domain_tools.capital_management_tool import execute_confirmed_capital_plan, preview_capital_change
from portfolio.cash_flow import list_cash_flows
from agent_control_center_utils import write_agent_fixture


def test_agent_capital_management_requires_confirmation_and_saves_flow(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, price=10.0)
    preview = preview_capital_change("u1", "deposit", 5000, "2026-06-12", output_dir=output_dir, db_path=db_path)
    assert preview.success
    executed = execute_confirmed_capital_plan(
        "u1",
        preview.data["plan_id"],
        preview.data["confirmation_token"],
        output_dir=output_dir,
        db_path=db_path,
    )
    assert executed.success
    flows = list_cash_flows("u1", db_path=db_path, output_dir=output_dir)
    assert flows[-1].amount == 5000
