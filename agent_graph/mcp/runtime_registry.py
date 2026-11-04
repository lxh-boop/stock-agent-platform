from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any

from agent_graph.workers.cards import EVIDENCE_COLLECTOR, PORTFOLIO_ANALYST
from agent_graph.mcp.config import resolve_mcp_server_configs
from agent_graph.mcp.discovery import discover_mcp_tools
from agent_graph.mcp.models import MCPServerConfig, MCPToolInfo
from agent_graph.toolbox import (
    OP_READ,
    TOOL_VISIBILITY_SYSTEM_PRIVATE,
    ToolDefinition,
    ToolRegistry,
    description,
)


@dataclass(frozen=True)
class MCPToolAdmissionPolicy:
    """Runtime-owned admission decision for one exact MCP capability.

    MCP discovery metadata is never authority. A Tool is callable only when an
    exact policy entry admits it and projects it through one or more registered
    Worker Tool IDs.
    """

    server_id: str
    tool_name: str
    allowed_caller_tool_ids: tuple[str, ...]
    allowed_agent_types: tuple[str, ...]
    projected_worker_tool_ids: tuple[str, ...]
    enabled: bool = True
    visibility: str = TOOL_VISIBILITY_SYSTEM_PRIVATE

    @property
    def tool_id(self) -> str:
        return f"mcp.{self.server_id}.{self.tool_name}"


@dataclass(frozen=True)
class MCPToolRuntimeRecord:
    tool_id: str
    server_id: str
    tool_name: str
    discovered: bool
    admitted: bool
    registered: bool
    projected: bool
    visibility: str
    projected_worker_tool_ids: tuple[str, ...] = ()
    allowed_caller_tool_ids: tuple[str, ...] = ()
    allowed_agent_types: tuple[str, ...] = ()
    input_schema_hash: str = ""
    output_schema_hash: str = ""
    admission_error: str = ""
    tool: MCPToolInfo | None = None
    server: MCPServerConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tool"] = self.tool.to_dict() if self.tool else None
        data["server"] = self.server.to_dict() if self.server else None
        return data


def _schema_hash(schema: dict[str, Any]) -> str:
    payload = json.dumps(schema or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _blocked_system_private_handler(
    _arguments: dict[str, Any],
    _context: dict[str, Any],
) -> dict[str, Any]:
    raise PermissionError("system_private_mcp_requires_registered_worker_adapter")


_ADMISSION_POLICIES: tuple[MCPToolAdmissionPolicy, ...] = (
    MCPToolAdmissionPolicy(
        server_id="rag",
        tool_name="search_news",
        allowed_caller_tool_ids=("evidence.search_news",),
        allowed_agent_types=(EVIDENCE_COLLECTOR,),
        projected_worker_tool_ids=("evidence.search_news",),
    ),
    MCPToolAdmissionPolicy(
        server_id="rag",
        tool_name="search_documents",
        allowed_caller_tool_ids=("evidence.search_rag",),
        allowed_agent_types=(EVIDENCE_COLLECTOR,),
        projected_worker_tool_ids=("evidence.search_rag",),
    ),
    MCPToolAdmissionPolicy(
        server_id="model",
        tool_name="predict_stock_score",
        allowed_caller_tool_ids=("internal.prediction.get_stock",),
        allowed_agent_types=(PORTFOLIO_ANALYST,),
        projected_worker_tool_ids=("internal.prediction.get_stock",),
    ),
    MCPToolAdmissionPolicy(
        server_id="model",
        tool_name="predict_rank",
        allowed_caller_tool_ids=("internal.ranking.get_latest",),
        allowed_agent_types=(PORTFOLIO_ANALYST,),
        projected_worker_tool_ids=("internal.ranking.get_latest",),
    ),
    MCPToolAdmissionPolicy(
        server_id="data",
        tool_name="get_portfolio_state",
        allowed_caller_tool_ids=(
            "internal.portfolio.get_state",
            "internal.account.get_state",
        ),
        allowed_agent_types=(PORTFOLIO_ANALYST,),
        projected_worker_tool_ids=(
            "internal.portfolio.get_state",
            "internal.account.get_state",
        ),
    ),
    MCPToolAdmissionPolicy(
        server_id="data",
        tool_name="get_user_profile",
        allowed_caller_tool_ids=("internal.user_profile.get",),
        allowed_agent_types=(PORTFOLIO_ANALYST,),
        projected_worker_tool_ids=("internal.user_profile.get",),
    ),
)


def admission_policies() -> dict[str, MCPToolAdmissionPolicy]:
    return {policy.tool_id: policy for policy in _ADMISSION_POLICIES}


class MCPRuntimeRegistry:
    """Canonical Runtime authority after Discovery and before execution."""

    def __init__(
        self,
        *,
        records: list[MCPToolRuntimeRecord],
        tool_registry: ToolRegistry,
    ) -> None:
        self._records = {record.tool_id: record for record in records}
        self.tool_registry = tool_registry

    def get(self, tool_id: str) -> MCPToolRuntimeRecord | None:
        return self._records.get(str(tool_id or ""))

    def list_records(self) -> list[MCPToolRuntimeRecord]:
        return list(self._records.values())

    def projected_worker_tool_ids(self, *, agent_type: str = "") -> list[str]:
        rows: list[str] = []
        for record in self._records.values():
            if not record.projected:
                continue
            if agent_type and agent_type not in set(record.allowed_agent_types):
                continue
            rows.extend(record.projected_worker_tool_ids)
        return list(dict.fromkeys(rows))

    def authorize(
        self,
        tool_id: str,
        *,
        caller_tool_id: str,
        agent_type: str,
    ) -> tuple[MCPToolRuntimeRecord | None, str]:
        record = self.get(tool_id)
        if record is None or not record.discovered:
            return None, "mcp_tool_not_discovered"
        if not record.admitted:
            return record, record.admission_error or "mcp_tool_not_admitted"
        if not record.registered:
            return record, "mcp_tool_not_registered"
        if not record.projected:
            return record, "mcp_tool_not_projected"
        if str(caller_tool_id or "") not in set(record.allowed_caller_tool_ids):
            return record, "mcp_caller_tool_not_allowed"
        if str(agent_type or "") not in set(record.allowed_agent_types):
            return record, "mcp_agent_type_not_allowed"
        return record, ""

    def report(self) -> dict[str, Any]:
        records = self.list_records()
        return {
            "authority": "runtime_registry",
            "raw_mcp_visibility": TOOL_VISIBILITY_SYSTEM_PRIVATE,
            "discovered_count": sum(1 for item in records if item.discovered),
            "admitted_count": sum(1 for item in records if item.admitted),
            "registered_count": sum(1 for item in records if item.registered),
            "projected_count": sum(1 for item in records if item.projected),
            "records": [item.to_dict() for item in records],
        }


def build_mcp_runtime_registry(
    context: dict[str, Any] | None = None,
    *,
    force_discovery: bool = False,
) -> MCPRuntimeRegistry:
    policies = admission_policies()
    servers = {item.server_id: item for item in resolve_mcp_server_configs(context)}
    discovered: dict[str, MCPToolInfo] = {}
    for result in discover_mcp_tools(context, force=force_discovery):
        if not result.success:
            continue
        for tool in result.tools:
            discovered[tool.namespaced_name] = tool

    definitions: list[ToolDefinition] = []
    records: list[MCPToolRuntimeRecord] = []
    for tool_id, tool in discovered.items():
        server = servers.get(tool.server_id)
        policy = policies.get(tool_id)
        error = ""
        admitted = True
        if policy is None:
            admitted = False
            error = "runtime_admission_policy_missing"
        elif not policy.enabled:
            admitted = False
            error = "runtime_admission_policy_disabled"
        elif server is None or not server.enabled:
            admitted = False
            error = "mcp_server_disabled"
        elif tool.tool_name not in set(server.allowed_tools):
            admitted = False
            error = "tool_not_in_runtime_allowlist"
        elif not server.read_only or not tool.tool_read_only:
            admitted = False
            error = "mcp_write_tool_blocked"

        registered = False
        projected = False
        visibility = policy.visibility if policy else TOOL_VISIBILITY_SYSTEM_PRIVATE
        if admitted and policy is not None:
            definition = ToolDefinition(
                name=tool_id,
                display_name=tool.tool_name,
                description=description(
                    tool.description or "Invoke one Runtime-admitted MCP capability.",
                    "A registered Worker Adapter requires this exact internal transport capability.",
                    "Direct LLM use, dynamic name-based dispatch, writes, or unregistered callers.",
                    "The MCP inputSchema validated by Runtime.",
                    "The MCP outputSchema validated before the Worker Adapter receives data.",
                ),
                input_schema=dict(tool.input_schema or {"type": "object", "properties": {}}),
                output_schema=dict(tool.output_schema or {"type": "object"}),
                execution_handler=_blocked_system_private_handler,
                supported_actions=["runtime_registered_mcp_transport"],
                supported_objects=["mcp_transport_capability"],
                operation_type=OP_READ,
                allowed_agent_types=list(policy.allowed_agent_types),
                permission_scope=OP_READ,
                runtime_policy={
                    "provider_type": "mcp",
                    "server_id": policy.server_id,
                    "transport_tool_name": policy.tool_name,
                    "visibility": policy.visibility,
                    "allowed_caller_tool_ids": list(policy.allowed_caller_tool_ids),
                    "projected_worker_tool_ids": list(policy.projected_worker_tool_ids),
                    "input_schema_hash": _schema_hash(tool.input_schema),
                    "output_schema_hash": _schema_hash(tool.output_schema),
                },
                visibility=policy.visibility,
                side_effects=[],
                mutates_business_state=False,
                idempotency="read_only",
                audit_level="full",
                tags=["mcp_transport", "system_private", "runtime_admitted"],
            )
            definitions.append(definition)
            registered = True
            projected = bool(policy.projected_worker_tool_ids)

        records.append(
            MCPToolRuntimeRecord(
                tool_id=tool_id,
                server_id=tool.server_id,
                tool_name=tool.tool_name,
                discovered=True,
                admitted=admitted,
                registered=registered,
                projected=projected,
                visibility=visibility,
                projected_worker_tool_ids=(
                    policy.projected_worker_tool_ids if policy and projected else ()
                ),
                allowed_caller_tool_ids=(policy.allowed_caller_tool_ids if policy else ()),
                allowed_agent_types=(policy.allowed_agent_types if policy else ()),
                input_schema_hash=_schema_hash(tool.input_schema),
                output_schema_hash=_schema_hash(tool.output_schema),
                admission_error=error,
                tool=tool,
                server=server,
            )
        )
    return MCPRuntimeRegistry(records=records, tool_registry=ToolRegistry(definitions))


def mcp_runtime_registry_report(
    context: dict[str, Any] | None = None,
    *,
    force_discovery: bool = False,
) -> dict[str, Any]:
    return build_mcp_runtime_registry(
        context,
        force_discovery=force_discovery,
    ).report()


__all__ = [
    "MCPRuntimeRegistry",
    "MCPToolAdmissionPolicy",
    "MCPToolRuntimeRecord",
    "admission_policies",
    "build_mcp_runtime_registry",
    "mcp_runtime_registry_report",
]
