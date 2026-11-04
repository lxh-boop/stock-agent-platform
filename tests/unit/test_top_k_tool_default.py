from agent_graph.services.top_k import resolve_requested_top_k


def test_top_k_tool_default() -> None:
    assert resolve_requested_top_k(tool_default_top_k=50, system_fallback_top_k=20) == 50
