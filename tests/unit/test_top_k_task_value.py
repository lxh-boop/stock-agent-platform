from agent_graph.services.top_k import resolve_requested_top_k


def test_top_k_task_value() -> None:
    assert resolve_requested_top_k(task_top_k="10", request_default_top_k=20, tool_default_top_k=50) == 10
