"""Single-agent orchestration (agent-as-tool pattern).

Graph shape::

    START -> agent -> END

``agent`` is the compiled ``create_agent`` subgraph added directly as a node,
so a write tool's ``interrupt()`` propagates through the parent checkpointer
and ``Command(resume=...)`` continues the loop inside the interrupted tool.
Domain read tools (evidence / internal data / graph relations / risk) and
write/proposal capabilities are plain tools on the agent — no supervisor,
no sub-agents, no specialist sub-loops.
"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from agent_graph.prompts import MAIN_AGENT_SYSTEM_PROMPT
from agent_graph.state import AgentState
from agent_graph.tools.external_mcp import build_research_docs_tools
from agent_graph.tools.main_tools import build_main_capability_tools, build_read_tools
from agent_graph.tools.runtime import AgentResources

_MAIN_RECURSION_LIMIT = 48

# Write tools gated by the official human-in-the-loop middleware (per-call
# interrupt; resume with {"type": "approve" | "reject", "message": str}).
GATED_WRITE_TOOLS = (
    "confirm_pending_proposal",
    "database_write_portfolio_graph_context",
    "database_write_evidence_graph_context",
)


def build_initial_messages(state: dict[str, Any]) -> list[HumanMessage]:
    """Seed the agent loop transcript from the run state."""

    parts = [f"用户请求：{str(state.get('query') or '')}"]
    if state.get("memory_summary"):
        parts.append("会话记忆摘要：" + str(state["memory_summary"])[:1200])
    parts.append(f"回复语言：{state.get('reply_language') or 'zh'}")
    return [HumanMessage(content="\n".join(parts))]


def build_agent_graph(
    model,
    resources: AgentResources,
    run_meta: dict[str, Any] | None = None,
    *,
    checkpointer: Any = None,
):
    """Compile the single-agent graph. One model + one resources bundle per run."""

    external_warnings: list[str] = []
    tools = [
        *build_read_tools(resources.tool_registry, resources.context),
        *build_main_capability_tools(resources.tool_registry, resources.context),
        *build_research_docs_tools(external_warnings),
    ]
    if external_warnings:
        run_meta.setdefault("warnings", []).extend(external_warnings)
    main_agent = create_agent(
        model,
        tools,
        system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={
                    name: {"allowed_decisions": ["approve", "reject"]}
                    for name in GATED_WRITE_TOOLS
                },
                interrupt_mode="per_call",
                description_prefix="写操作需要用户批准后才会执行",
            )
        ],
    )
    builder = StateGraph(AgentState)
    builder.add_node("agent", main_agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    return builder.compile(checkpointer=checkpointer)


__all__ = ["build_agent_graph", "build_initial_messages"]
