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
    mcp_context: dict[str, Any] | None = None,
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
    from agent_graph.mcp.runtime_registry import build_mcp_runtime_registry

    projected = set(
        build_mcp_runtime_registry(mcp_context).projected_worker_tool_ids()
    )
    admitted_definitions = [
        definition
        for definition in definitions
        if str((definition.runtime_policy or {}).get("provider_type") or "") != "mcp"
        or definition.name in projected
    ]
    return ToolRegistry(admitted_definitions)
