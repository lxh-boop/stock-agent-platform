from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class MCPServerConfig:
    server_id: str
    name: str
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str = ""
    endpoint: str = ""
    enabled: bool = False
    read_only: bool = True
    allowed_tools: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    environment_key_names: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["args"] = list(self.args)
        data["allowed_tools"] = list(self.allowed_tools)
        data["environment_key_names"] = list(self.environment_key_names)
        return data


@dataclass(frozen=True)
class MCPToolInfo:
    server_id: str
    server_name: str
    tool_name: str
    namespaced_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)
    discovery_status: str = "discovered"
    server_enabled: bool = False
    server_read_only: bool = True
    tool_read_only: bool = True
    allowlisted: bool = False
    mapped: bool = False
    mapping_error: str = ""
    discovered_at: str = ""
    transport: str = "stdio"
    timeout_seconds: float = 30.0
    effective_read_only: bool = True
    effective_permission: str = "read"
    effective_allowed_agents: tuple[str, ...] = ()
    requires_confirmation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["effective_allowed_agents"] = list(self.effective_allowed_agents)
        return data


@dataclass(frozen=True)
class MCPDiscoveryResult:
    server_id: str
    server_name: str
    enabled: bool
    transport: str
    success: bool
    discovered_at: str
    tools: tuple[MCPToolInfo, ...] = ()
    error: str = ""
    cached: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tools"] = [tool.to_dict() for tool in self.tools]
        return data
