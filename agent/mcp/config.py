from __future__ import annotations

import os
from importlib import metadata as importlib_metadata
from pathlib import Path
import sys
from typing import Any

from agent.mcp.models import MCPServerConfig


DATA_SERVER_ID = "data"
DATA_TOOL_NAMES = (
    "get_user_profile",
    "get_portfolio_state",
    "get_positions",
    "get_orders",
    "get_stock_info",
    "get_latest_ranking",
    "get_latest_recommendations",
)
RAG_SERVER_ID = "rag"
RAG_TOOL_NAMES = (
    "search_documents",
    "search_news",
    "retrieve_evidence",
)
MODEL_SERVER_ID = "model"
MODEL_TOOL_NAMES = (
    "predict_stock_score",
    "predict_rank",
    "predict_risk",
)
DEFAULT_DISCOVERY_TTL_SECONDS = 300


def mcp_sdk_version() -> str:
    try:
        return importlib_metadata.version("mcp")
    except Exception:
        return "not-installed"


def _bool(value: Any, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    return default


def _tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return ()


def data_server_config(
    *,
    enabled: bool = True,
    allowed_tools: tuple[str, ...] | None = None,
    timeout_seconds: float = 30.0,
) -> MCPServerConfig:
    project_root = Path(__file__).resolve().parents[2]
    return MCPServerConfig(
        server_id=DATA_SERVER_ID,
        name="Stock Daily Internal Data MCP",
        transport="stdio",
        command=sys.executable,
        args=("-m", "agent.mcp.servers.data_server"),
        cwd=str(project_root),
        enabled=bool(enabled),
        read_only=True,
        allowed_tools=allowed_tools or DATA_TOOL_NAMES,
        timeout_seconds=float(timeout_seconds or 30.0),
        metadata={
            "provider": "internal",
            "project_managed": True,
            "sdk_version": mcp_sdk_version(),
            "purpose": "authoritative_read_only_application_data",
        },
    )


def rag_server_config(
    *,
    enabled: bool = True,
    allowed_tools: tuple[str, ...] | None = None,
    timeout_seconds: float = 90.0,
) -> MCPServerConfig:
    project_root = Path(__file__).resolve().parents[2]
    return MCPServerConfig(
        server_id=RAG_SERVER_ID,
        name="Stock Daily Internal RAG MCP",
        transport="stdio",
        command=sys.executable,
        args=("-m", "agent.mcp.servers.rag_server"),
        cwd=str(project_root),
        enabled=bool(enabled),
        read_only=True,
        allowed_tools=allowed_tools or RAG_TOOL_NAMES,
        timeout_seconds=float(timeout_seconds or 90.0),
        metadata={
            "provider": "internal",
            "project_managed": True,
            "sdk_version": mcp_sdk_version(),
            "purpose": "hybrid_rag_evidence",
            "retrieval_implementation": "bm25_dense_rrf_reranker",
        },
    )


def model_server_config(
    *,
    enabled: bool = True,
    allowed_tools: tuple[str, ...] | None = None,
    timeout_seconds: float = 30.0,
) -> MCPServerConfig:
    project_root = Path(__file__).resolve().parents[2]
    return MCPServerConfig(
        server_id=MODEL_SERVER_ID,
        name="Stock Daily Internal Model MCP",
        transport="stdio",
        command=sys.executable,
        args=("-m", "agent.mcp.servers.model_server"),
        cwd=str(project_root),
        enabled=bool(enabled),
        read_only=True,
        allowed_tools=allowed_tools or MODEL_TOOL_NAMES,
        timeout_seconds=float(timeout_seconds or 30.0),
        metadata={
            "provider": "internal",
            "project_managed": True,
            "sdk_version": mcp_sdk_version(),
            "purpose": "completed_active_ranker_inference_snapshots",
            "long_running_execution": "task_runtime",
        },
    )


def _server_from_dict(data: dict[str, Any]) -> MCPServerConfig:
    return MCPServerConfig(
        server_id=str(data.get("server_id") or "").strip(),
        name=str(data.get("name") or data.get("server_id") or "").strip(),
        transport=str(data.get("transport") or "stdio").strip(),
        command=str(data.get("command") or ""),
        args=_tuple(data.get("args")),
        cwd=str(data.get("cwd") or ""),
        endpoint=str(data.get("endpoint") or ""),
        enabled=_bool(data.get("enabled"), False),
        read_only=_bool(data.get("read_only"), True),
        allowed_tools=_tuple(data.get("allowed_tools")),
        timeout_seconds=float(data.get("timeout_seconds") or 30.0),
        environment_key_names=_tuple(data.get("environment_key_names")),
        metadata=dict(data.get("metadata") or {}),
    )


def external_server_configs(
    local_cfg: dict[str, Any] | None,
) -> list[MCPServerConfig]:
    """Parse future external MCP registrations without connecting to them.

    Phase 5 deliberately ships no endpoint. Registrations default to disabled,
    require an explicit tool allowlist, and will fail closed if a future caller
    enables a transport that the client has not implemented yet.
    """

    registrations = (local_cfg or {}).get("mcp_external_servers") or []
    servers: list[MCPServerConfig] = []
    for index, raw in enumerate(registrations):
        if not isinstance(raw, dict):
            raise ValueError(f"invalid_external_mcp_registration:{index}")
        data = dict(raw)
        data.setdefault("enabled", False)
        data.setdefault("read_only", True)
        metadata = dict(data.get("metadata") or {})
        metadata.update({"provider": "external", "project_managed": False})
        data["metadata"] = metadata
        server = _server_from_dict(data)
        if not server.server_id:
            raise ValueError(f"external_mcp_server_id_required:{index}")
        if server.enabled and not server.allowed_tools:
            raise ValueError(
                f"external_mcp_tool_allowlist_required:{server.server_id}"
            )
        servers.append(server)
    return servers


def build_mcp_context_from_local_config(local_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    if local_cfg is None:
        try:
            from local_config import load_local_config

            local_cfg = load_local_config()
        except Exception:
            local_cfg = {}

    env_enabled = _bool(os.environ.get("STOCK_APP_MCP_DATA_ENABLED"), True)
    enabled = _bool((local_cfg or {}).get("mcp_data_enabled"), env_enabled)
    allowed = _tuple((local_cfg or {}).get("mcp_data_allowed_tools")) or DATA_TOOL_NAMES
    timeout_seconds = float((local_cfg or {}).get("mcp_data_timeout_seconds") or 30.0)
    rag_enabled = _bool((local_cfg or {}).get("mcp_rag_enabled"), True)
    rag_allowed = _tuple((local_cfg or {}).get("mcp_rag_allowed_tools")) or RAG_TOOL_NAMES
    rag_timeout_seconds = float((local_cfg or {}).get("mcp_rag_timeout_seconds") or 90.0)
    model_enabled = _bool((local_cfg or {}).get("mcp_model_enabled"), True)
    model_allowed = _tuple((local_cfg or {}).get("mcp_model_allowed_tools")) or MODEL_TOOL_NAMES
    model_timeout_seconds = float((local_cfg or {}).get("mcp_model_timeout_seconds") or 30.0)
    return {
        "servers": [
            data_server_config(
                enabled=enabled,
                allowed_tools=allowed,
                timeout_seconds=timeout_seconds,
            ).to_dict(),
            rag_server_config(
                enabled=rag_enabled,
                allowed_tools=rag_allowed,
                timeout_seconds=rag_timeout_seconds,
            ).to_dict(),
            model_server_config(
                enabled=model_enabled,
                allowed_tools=model_allowed,
                timeout_seconds=model_timeout_seconds,
            ).to_dict(),
            *[
                server.to_dict()
                for server in external_server_configs(local_cfg)
            ],
        ],
        "discovery_ttl_seconds": int((local_cfg or {}).get("mcp_discovery_ttl_seconds") or DEFAULT_DISCOVERY_TTL_SECONDS),
        "enabled": bool(enabled or rag_enabled or model_enabled),
    }


def resolve_mcp_server_configs(context: dict[str, Any] | None = None) -> list[MCPServerConfig]:
    context = dict(context or {})
    mcp_context = context.get("mcp") if isinstance(context.get("mcp"), dict) else context
    raw_servers = mcp_context.get("servers") if isinstance(mcp_context, dict) else None
    if raw_servers is None:
        local = build_mcp_context_from_local_config()
        raw_servers = local.get("servers")

    servers: list[MCPServerConfig] = []
    for raw in raw_servers or []:
        if isinstance(raw, MCPServerConfig):
            servers.append(raw)
        elif isinstance(raw, dict):
            server = _server_from_dict(raw)
            if server.server_id:
                servers.append(server)
    if not servers:
        servers.append(data_server_config(enabled=True))
    return servers


def discovery_ttl_seconds(context: dict[str, Any] | None = None) -> int:
    context = dict(context or {})
    mcp_context = context.get("mcp") if isinstance(context.get("mcp"), dict) else context
    try:
        return max(1, int((mcp_context or {}).get("discovery_ttl_seconds") or DEFAULT_DISCOVERY_TTL_SECONDS))
    except Exception:
        return DEFAULT_DISCOVERY_TTL_SECONDS
