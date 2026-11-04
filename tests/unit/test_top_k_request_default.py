from agent_graph.services.top_k import resolve_requested_top_k


def test_top_k_request_default() -> None:
    assert resolve_requested_top_k(request_default_top_k=10, tool_default_top_k=50) == 10
