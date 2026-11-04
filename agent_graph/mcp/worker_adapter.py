from __future__ import annotations

from typing import Any

from agent_graph.mcp.client_manager import call_mcp_tool


def invoke_worker_mcp(
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    context: dict[str, Any],
    *,
    caller_tool_id: str,
) -> dict[str, Any]:
    """Invoke one admitted system-private MCP capability for an exact Worker Tool."""

    result = call_mcp_tool(
        f"mcp.{server_id}.{tool_name}",
        dict(arguments or {}),
        context=dict(context or {}),
        caller_tool_id=str(caller_tool_id or ""),
        agent_type=str((context or {}).get("agent_role") or ""),
    )
    return result.to_dict()
