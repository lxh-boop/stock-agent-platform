from __future__ import annotations

from agent_graph.domain_tools.scheduler_tool import query_scheduler_status


def test_agent_scheduler_tool_is_non_crashing(tmp_path) -> None:
    result = query_scheduler_status(root=tmp_path)
    assert result["status"] in {"success", "missing_status"}
    assert "latest_job_status" in result
