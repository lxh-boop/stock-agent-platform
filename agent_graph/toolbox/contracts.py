"""Stable data contracts and authorization constants for registered tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal
from uuid import uuid4


OP_READ = "read"
OP_PROPOSAL = "proposal"
OP_WRITE = "write"
OP_SYSTEM = "system"

AGENT_MAIN = "main_agent"
AGENT_READ = "read_worker"
AGENT_WRITE = "write_worker"

TOOL_RESULT_SCHEMA_VERSION = "tool-result-v1"
TOOL_IO_CONTRACT_VERSION = "tool-io-contract.v2"

TOOL_VISIBILITY_PUBLIC = "public"
TOOL_VISIBILITY_WORKER_PRIVATE = "worker_private"
TOOL_VISIBILITY_SYSTEM_PRIVATE = "system_private"
TOOL_VISIBILITIES = frozenset(
    {
        TOOL_VISIBILITY_PUBLIC,
        TOOL_VISIBILITY_WORKER_PRIVATE,
        TOOL_VISIBILITY_SYSTEM_PRIVATE,
    }
)


@dataclass(frozen=True)
class ToolError:
    """Minimal Tool-to-Worker error contract.

    Runtime context already identifies the run, task, Worker and Tool call, so
    only the stable error id, attempted operation and safe reason are returned
    to the owning Worker.
    """

    error_id: str
    operation: str
    reason: str

    @classmethod
    def create(
        cls,
        *,
        error_id: str,
        operation: str,
        reason: str,
    ) -> "ToolError":
        return cls(
            error_id=str(error_id or "tool_failure")[:120],
            operation=str(operation or "tool_operation")[:500],
            reason=str(reason or "Tool execution failed.")[:2000],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UnifiedToolResult:
    """Normalized result returned by every registered tool execution."""

    success: bool
    tool_name: str
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    error_type: str = ""
    error_message: str = ""
    error: ToolError | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    artifact_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    retry_count: int = 0
    circuit_state: str = ""
    schema_version: str = TOOL_RESULT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolInputContract:
    """Semantic input slot visible to a Worker-private Tool planner.

    Runtime transport details stay out of this contract. A Tool declares only
    its own input name/type requirements; it never names a concrete producer.
    ``cardinality="many"`` means the DAG must bind a non-empty list of semantic
    values. In that case ``schema_id`` describes each list element.
    """

    slot_id: str
    schema_id: str = ""
    required: bool = False
    accepted_sources: tuple[str, ...] = ("context", "upstream_tool")
    cardinality: Literal["one", "many"] = "one"
    description: str = ""
    contract: str = ""
    version: str = "1.0"
    accepted_versions: tuple[str, ...] = ()

    def contract_descriptor(self) -> dict[str, Any]:
        contract = str(self.contract or self.slot_id).strip()
        version = str(self.version or "1.0").strip()
        schema_id = str(self.schema_id or f"{contract}@{version}").strip()
        accepted = tuple(
            str(item).strip()
            for item in (self.accepted_versions or (version,))
            if str(item).strip()
        )
        return {
            "contract": contract,
            "version": version,
            "schema_id": schema_id,
            "accepted_versions": list(accepted),
        }

    def planner_view(self) -> dict[str, Any]:
        descriptor = self.contract_descriptor()
        return {
            "slot_id": str(self.slot_id),
            "contract": descriptor["contract"],
            "version": descriptor["version"],
            "schema_id": descriptor["schema_id"],
            "accepted_versions": descriptor["accepted_versions"],
            "required": bool(self.required),
            "accepted_sources": list(self.accepted_sources or ()),
            "cardinality": str(self.cardinality),
            "description": str(self.description or ""),
        }


@dataclass(frozen=True)
class ToolOutputContract:
    """Semantic output slot plus Runtime-only extraction metadata.

    ``source_path`` is intentionally omitted from ``planner_view``.  It maps a
    Tool's concrete Python return shape (for example ``data.records``) to a
    stable semantic slot (for example ``market_ranking_signals``).
    """

    slot_id: str
    schema_id: str = ""
    source_path: str = ""
    description: str = ""
    provenance_required: bool = True
    contract: str = ""
    version: str = "1.0"

    def contract_descriptor(self) -> dict[str, Any]:
        contract = str(self.contract or self.slot_id).strip()
        version = str(self.version or "1.0").strip()
        schema_id = str(self.schema_id or f"{contract}@{version}").strip()
        return {
            "contract": contract,
            "version": version,
            "schema_id": schema_id,
        }

    def planner_view(self) -> dict[str, Any]:
        descriptor = self.contract_descriptor()
        return {
            "slot_id": str(self.slot_id),
            "contract": descriptor["contract"],
            "version": descriptor["version"],
            "schema_id": descriptor["schema_id"],
            "description": str(self.description or ""),
            "provenance_required": bool(self.provenance_required),
        }


@dataclass(frozen=True)
class ToolDefinition:
    """Declarative contract for one atomic domain capability."""

    name: str
    display_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execution_handler: Callable[[dict[str, Any], dict[str, Any]], Any]
    supported_actions: list[str] = field(default_factory=list)
    supported_objects: list[str] = field(default_factory=list)
    produced_outputs: list[str] = field(default_factory=list)
    required_input_slots: list[str] = field(default_factory=list)
    optional_input_slots: list[str] = field(default_factory=list)
    input_contracts: list[ToolInputContract] = field(default_factory=list)
    output_contracts: list[ToolOutputContract] = field(default_factory=list)
    operation_type: str = OP_READ
    allowed_agent_types: list[str] = field(default_factory=lambda: [AGENT_MAIN, AGENT_READ])
    permission_scope: str = OP_READ
    requires_approval: bool = False
    runtime_policy: dict[str, Any] = field(default_factory=dict)
    version: str = "1"
    enabled: bool = True
    sensitivity: str = "normal"
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    visibility: str = TOOL_VISIBILITY_PUBLIC
    side_effects: list[str] = field(default_factory=list)
    mutates_business_state: bool = False
    idempotency: str = "unspecified"
    audit_level: str = "standard"

    def public_view(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("execution_handler", None)
        # Runtime-only field mappings must never leak into planner/public views.
        data["output_contracts"] = [item.planner_view() for item in self.output_contracts]
        data["input_contracts"] = [item.planner_view() for item in self.input_contracts]
        return data
