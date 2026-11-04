from __future__ import annotations

import time
from typing import Any

from agent_graph.mcp.runtime_registry import build_mcp_runtime_registry
from agent_graph.mcp.schema_adapter import validate_arguments, validate_payload_schema
from agent_graph.mcp.security import redact_sensitive, safe_external_payload
from agent_graph.mcp.transport import call_stdio_tool
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult


_CALL_COUNT: dict[str, int] = {}


def parse_mcp_tool_name(namespaced_name: str) -> tuple[str, str]:
    parts = str(namespaced_name or "").split(".", 2)
    if len(parts) != 3 or parts[0] != "mcp" or not parts[1] or not parts[2]:
        raise ValueError(f"invalid_mcp_tool_name:{namespaced_name}")
    return parts[1], parts[2]


def call_mcp_tool(
    namespaced_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    context: dict[str, Any] | None = None,
    caller_tool_id: str = "",
    agent_type: str = "",
) -> ToolResult:
    context = dict(context or {})
    server_id, tool_name = parse_mcp_tool_name(namespaced_name)
    runtime_registry = build_mcp_runtime_registry(context)
    record, authorization_error = runtime_registry.authorize(
        namespaced_name,
        caller_tool_id=str(caller_tool_id or ""),
        agent_type=str(agent_type or ""),
    )
    if record is None or record.tool is None or record.server is None:
        return ToolResult(
            success=False,
            message="MCP tool is unavailable to the Runtime Registry.",
            data={
                "status": "unavailable",
                "provider_type": "mcp",
                "server_id": server_id,
                "tool_name": tool_name,
                "runtime_authority": "runtime_registry",
            },
            warnings=[],
            errors=[authorization_error or f"mcp_tool_unavailable:{namespaced_name}"],
            permission=ToolPermission.READ,
            tool_name=namespaced_name,
        )
    if authorization_error:
        return ToolResult(
            success=False,
            message="MCP tool is blocked by the Runtime Registry.",
            data={
                "status": "blocked",
                "provider_type": "mcp",
                "server_id": server_id,
                "tool_name": tool_name,
                "runtime_authority": "runtime_registry",
                "caller_tool_id": str(caller_tool_id or ""),
                "agent_type": str(agent_type or ""),
            },
            errors=[authorization_error],
            permission=ToolPermission.READ,
            tool_name=namespaced_name,
        )
    tool = record.tool

    ok, errors = validate_arguments(tool.input_schema, dict(arguments or {}))
    if not ok:
        return ToolResult(
            success=False,
            message="MCP argument validation failed.",
            data={
                "status": "validation_failed",
                "provider_type": "mcp",
                "server_id": server_id,
                "tool_name": tool_name,
                "fallback_recommended": True,
                "call_attempted": False,
            },
            errors=[f"mcp_args_invalid:{','.join(errors)}"],
            permission=ToolPermission.READ,
            tool_name=namespaced_name,
        )

    failure_mode = str(context.get("mcp_fail_mode") or "").strip().lower()
    if failure_mode == "dependency":
        raise RuntimeError("dependency_error:simulated_mcp_context_failure")
    if failure_mode == "timeout":
        time.sleep(float(context.get("mcp_timeout_sleep_seconds") or 2.0))

    _CALL_COUNT[namespaced_name] = _CALL_COUNT.get(namespaced_name, 0) + 1
    server = record.server
    raw = call_stdio_tool(
        server,
        tool_name,
        dict(arguments or {}),
        context=context,
    )

    output_valid, output_errors = validate_payload_schema(tool.output_schema, raw)
    if not output_valid:
        return ToolResult(
            success=False,
            message="MCP outputSchema validation failed.",
            data={
                "status": "output_validation_failed",
                "provider_type": "mcp",
                "server_id": server_id,
                "tool_name": tool_name,
                "runtime_authority": "runtime_registry",
                "call_attempted": True,
            },
            errors=[f"mcp_output_schema_invalid:{','.join(output_errors)}"],
            permission=ToolPermission.READ,
            tool_name=namespaced_name,
        )

    internal_provider = str(server.metadata.get("provider") or "").lower() == "internal"
    payload = (
        redact_sensitive(raw, max_chars=5000)
        if internal_provider
        else safe_external_payload(raw, max_chars=5000)
    )
    data = payload.get("data") if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("provider_type", "mcp")
    data.setdefault("server_id", server_id)
    data.setdefault("tool_name", tool_name)
    data.setdefault("transport", tool.transport)
    data.setdefault("runtime_authority", "runtime_registry")
    data.setdefault("caller_tool_id", str(caller_tool_id or ""))
    data.setdefault("agent_type", str(agent_type or ""))
    data.setdefault("input_schema_hash", record.input_schema_hash)
    data.setdefault("output_schema_hash", record.output_schema_hash)
    data.setdefault(
        "untrusted_evidence",
        not internal_provider,
    )
    return ToolResult(
        success=bool(payload.get("success")) if isinstance(payload, dict) else False,
        message=str(payload.get("message") or "") if isinstance(payload, dict) else "",
        data=data,
        warnings=list(payload.get("warnings") or []) if isinstance(payload, dict) else [],
        errors=list(payload.get("errors") or []) if isinstance(payload, dict) else ["invalid_mcp_payload"],
        permission=ToolPermission.READ,
        tool_name=namespaced_name,
    )


def call_stats() -> dict[str, int]:
    return dict(_CALL_COUNT)


def reset_call_stats() -> None:
    _CALL_COUNT.clear()
