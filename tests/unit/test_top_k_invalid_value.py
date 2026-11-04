from agent_graph.services.top_k import resolve_requested_top_k


def test_top_k_invalid_value() -> None:
    assert resolve_requested_top_k(user_explicit_top_k="invalid", request_default_top_k=10, tool_default_top_k=50) == 10
    assert resolve_requested_top_k(user_explicit_top_k=0, request_default_top_k=10, tool_default_top_k=50) == 10
