from __future__ import annotations

from typing import Any

from agent_graph.mcp.runtime_registry import build_mcp_runtime_registry


def projected_worker_tool_ids(
    context: dict[str, Any] | None = None,
    *,
    agent_type: str = "",
) -> list[str]:
    """Return only Runtime-approved Worker IDs; raw MCP IDs are system-private."""

    return build_mcp_runtime_registry(context).projected_worker_tool_ids(
        agent_type=str(agent_type or ""),
    )


def runtime_registry_report(
    context: dict[str, Any] | None = None,
    *,
    force_discovery: bool = False,
) -> dict[str, Any]:
    return build_mcp_runtime_registry(
        context,
        force_discovery=force_discovery,
    ).report()


def _provider_metadata(call: dict[str, Any]) -> dict[str, Any]:
    metadata = call.get("metadata") if isinstance(call.get("metadata"), dict) else {}
    provider = metadata.get("provider") if isinstance(metadata.get("provider"), dict) else {}
    if provider:
        return dict(provider)
    result = call.get("result") if isinstance(call.get("result"), dict) else {}
    result_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    nested = result_metadata.get("provider") if isinstance(result_metadata.get("provider"), dict) else {}
    return dict(nested)


def summarize_mcp_usage(agent_result: dict[str, Any] | None) -> dict[str, Any]:
    """Summarize explicit provider metadata without inferring semantics from names."""

    data = dict(agent_result or {})
    orchestration = data.get("orchestration") if isinstance(data.get("orchestration"), dict) else {}
    nested = data.get("result") if isinstance(data.get("result"), dict) else {}
    if isinstance(nested.get("data"), dict):
        orchestration = nested.get("data") or orchestration
    calls: list[dict[str, Any]] = []
    for call in orchestration.get("tool_calls") or data.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        provider = _provider_metadata(call)
        if str(provider.get("provider_type") or "") != "mcp":
            continue
        reliability = dict(call.get("runtime_reliability") or {})
        calls.append(
            {
                "worker_tool_id": str(call.get("tool_name") or call.get("intent") or ""),
                "server_id": str(provider.get("server_id") or ""),
                "transport_tool_name": str(provider.get("transport_tool_name") or ""),
                "success": bool(call.get("success")),
                "elapsed_ms": reliability.get("elapsed_ms"),
                "retry_count": reliability.get("retry_count"),
                "circuit_state": reliability.get("circuit_state"),
                "error_type": reliability.get("error_type"),
            }
        )
    return {
        "used_mcp": bool(calls),
        "mcp_tool_calls": calls,
        "raw_mcp_tools_exposed_to_llm": False,
        "runtime_authority": "runtime_registry",
    }


__all__ = [
    "projected_worker_tool_ids",
    "runtime_registry_report",
    "summarize_mcp_usage",
]
