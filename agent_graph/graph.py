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
from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from agent_graph.prompts import MAIN_AGENT_SYSTEM_PROMPT
from agent_graph.state import AgentState
from agent_graph.tools.main_tools import build_main_capability_tools, build_read_tools
from agent_graph.tools.runtime import AgentResources

_MAIN_RECURSION_LIMIT = 48


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

    tools = [
        *build_read_tools(resources.tool_registry, resources.context),
        *build_main_capability_tools(resources.tool_registry, resources.context),
    ]
    main_agent = create_agent(model, tools, system_prompt=MAIN_AGENT_SYSTEM_PROMPT)
    builder = StateGraph(AgentState)
    builder.add_node("agent", main_agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    return builder.compile(checkpointer=checkpointer)


__all__ = ["build_agent_graph", "build_initial_messages"]
