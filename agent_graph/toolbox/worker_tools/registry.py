"""Assemble the worker-tool registry (read + graph-write contracts, MCP-admitted)."""

from __future__ import annotations

from typing import Any

from agent_graph.financial_graph.impact_service import GraphImpactService
from agent_graph.financial_graph.provider_adapter import GraphProviderAdapter
from agent_graph.toolbox import ToolRegistry


def build_worker_tool_registry(
    *,
    provider: GraphProviderAdapter,
    impact_service: GraphImpactService | None = None,
) -> ToolRegistry:
    from .evidence import build_evidence_tool_definitions
    from .graph_context import build_graph_context_tool_definitions
    from .graph_relation import build_graph_relation_tool_definitions
    from .internal_system import build_internal_system_tool_definitions

    graph_relation_tools = (
        build_graph_relation_tool_definitions(impact_service)
        if impact_service is not None
        else []
    )
    definitions = [
        *build_evidence_tool_definitions(provider),
        *build_internal_system_tool_definitions(provider),
        *build_graph_context_tool_definitions(provider),
        *graph_relation_tools,
    ]
    return ToolRegistry(definitions)
