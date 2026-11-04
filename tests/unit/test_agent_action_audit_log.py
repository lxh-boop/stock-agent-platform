from __future__ import annotations

from agent_graph.domain_tools.audit_tool import read_agent_action_logs, write_agent_action_log


def test_agent_action_audit_log_writes_jsonl(tmp_path) -> None:
    output_dir = tmp_path / "outputs"
    write_agent_action_log(
        "u1",
        intent="stock_analysis",
        tool_name="stock_analysis",
        tool_input={"stock_code": "600519"},
        tool_output_summary={"ok": True},
        output_dir=output_dir,
        db_path=tmp_path / "agent.db",
    )
    rows = read_agent_action_logs("u1", output_dir=output_dir)
    assert rows
    assert rows[-1]["tool_name"] == "stock_analysis"
