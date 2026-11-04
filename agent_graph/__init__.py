"""LangGraph-based agent engine (parallel rebuild of the self-built runtime).

The public entry is :func:`agent_graph.service.run_langgraph_agent_request`, which
mirrors the response contract of ``agent.executor.run_agent_request``. The legacy
engine under ``agent/`` stays untouched and remains the default until the
``agent_engine`` setting is switched to ``"langgraph"``.
"""

from agent_graph.llm import build_chat_model, generate_structured

__all__ = ["build_chat_model", "generate_structured", "run_langgraph_agent_request"]


def __getattr__(name: str):  # lazy re-export avoids heavy import at package load
    if name == "run_langgraph_agent_request":
        from agent_graph.service import run_langgraph_agent_request

        return run_langgraph_agent_request
    raise AttributeError(name)
