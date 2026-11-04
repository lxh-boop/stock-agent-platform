"""Worker-to-tools binding for the LangGraph engine.

Tool visibility keeps the legacy contract: a worker only ever sees the tools
declared on its card (``CapabilityWorkerDirectory.private_tool_ids``); the
supervisor never sees tools at all.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from agent_graph.workers.cards import CapabilityWorkerDirectory
from agent_graph.toolbox.registry import ToolRegistry

from agent_graph.tools.adapter import sanitize_tool_name, wrap_tool_definition


def build_worker_tools(
    registry: ToolRegistry,
    context: dict[str, object],
    worker_id: str,
) -> list[BaseTool]:
    """Bind the card-declared tools of one worker to LangChain tool objects."""

    directory = CapabilityWorkerDirectory()
    tools: list[BaseTool] = []
    for tool_id in directory.private_tool_ids(worker_id):
        definition = registry.get(tool_id)
        if definition is None or not definition.enabled:
            continue
        tools.append(wrap_tool_definition(definition, dict(context), worker_id=worker_id))
    return tools


def tool_id_to_name(tool_id: str) -> str:
    return sanitize_tool_name(tool_id)


__all__ = ["build_worker_tools", "tool_id_to_name"]
