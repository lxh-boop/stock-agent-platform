"""Run-scoped registry and progressive private Tool catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_graph.financial_graph.impact_service import GraphImpactService
from agent_graph.financial_graph.provider_adapter import GraphProviderAdapter
from agent_graph.toolbox import TOOL_VISIBILITY_WORKER_PRIVATE, ToolRegistry
from agent_graph.toolbox.validation import input_contracts_for, output_contracts_for
from agent_graph.toolbox import TOOL_IO_CONTRACT_VERSION



@dataclass(frozen=True)
class WorkerToolDirectory:
    """Private role-to-Tool projection with progressive disclosure."""

    registry: ToolRegistry

    def allowed_tool_names(self, worker_role: str) -> list[str]:
        return [
            definition.name
            for definition in self.registry.list(
                agent_type=str(worker_role or ""),
                visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            )
        ]

    def candidate_tool_names(
        self,
        worker_role: str,
        *,
        allowed_tool_names: list[str] | set[str] | None = None,
    ) -> list[str]:
        """Return the Worker-private Tool catalog allowed for DAG planning.

        Runtime intentionally does not pre-bind Tool inputs to context keys or
        upstream output names here. Concrete producer/consumer relationships
        belong only to the Tool DAG and are validated after the DAG is produced.
        """

        allowed = {str(item) for item in (allowed_tool_names or []) if str(item)}
        return [
            definition.name
            for definition in self.registry.list(
                agent_type=str(worker_role or ""),
                visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            )
            if not allowed or definition.name in allowed
        ]

    def allows(self, worker_role: str, tool_name: str) -> bool:
        definition = self.registry.get(tool_name)
        return bool(
            definition
            and definition.visibility == TOOL_VISIBILITY_WORKER_PRIVATE
            and str(worker_role or "") in set(definition.allowed_agent_types)
        )

    @staticmethod
    def _short_description(value: str) -> str:
        text = " ".join(str(value or "").split())
        for marker in ("。", ". ", "\n"):
            if marker in text:
                text = text.split(marker, 1)[0]
                break
        return text[:220]

    def semantic_output_slots(
        self,
        worker_role: str,
        *,
        tool_names: list[str] | set[str] | None = None,
    ) -> list[str]:
        """Return public semantic output keys without exposing private Tool identities."""

        selected = {str(item) for item in (tool_names or []) if str(item)}
        slots: list[str] = []
        for definition in self.registry.list(
            agent_type=str(worker_role or ""),
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
        ):
            if selected and definition.name not in selected:
                continue
            slots.extend(
                str(item.slot_id)
                for item in output_contracts_for(definition)
                if str(item.slot_id)
            )
        return list(dict.fromkeys(slots))

    def summary_catalog(
        self,
        worker_role: str,
        *,
        tool_names: list[str] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        selected = {str(item) for item in (tool_names or []) if str(item)}
        rows: list[dict[str, Any]] = []
        for definition in self.registry.list(
            agent_type=str(worker_role or ""),
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
        ):
            if selected and definition.name not in selected:
                continue
            input_contracts = input_contracts_for(definition)
            output_contracts = output_contracts_for(definition)
            rows.append({
                "tool_id": definition.name,
                "short_description": self._short_description(definition.description),
                "input_contract": [item.planner_view() for item in input_contracts],
                "output_contract": [item.planner_view() for item in output_contracts],
                "required_input_slots": [item.slot_id for item in input_contracts if item.required],
                "optional_input_slots": [item.slot_id for item in input_contracts if not item.required],
                "produced_output_slots": [item.slot_id for item in output_contracts],
                "effect_limit": definition.operation_type,
                "side_effect_count": len(definition.side_effects or []),
                "io_contract_version": TOOL_IO_CONTRACT_VERSION,
            })
        return rows

    def load_details(self, worker_role: str, tool_ids: list[str]) -> list[dict[str, Any]]:
        requested = list(dict.fromkeys(str(item) for item in tool_ids if str(item)))
        rows: list[dict[str, Any]] = []
        for tool_id in requested:
            definition = self.registry.get(tool_id)
            if definition is None or not self.allows(worker_role, tool_id):
                raise KeyError(f"worker_private_tool_not_available:{worker_role}:{tool_id}")
            semantic_inputs = input_contracts_for(definition)
            semantic_outputs = output_contracts_for(definition)
            rows.append({
                "tool_id": definition.name,
                "description": definition.description,
                "input_schema": dict(definition.input_schema or {}),
                "input_contract": [item.planner_view() for item in semantic_inputs],
                "output_contract": [item.planner_view() for item in semantic_outputs],
                "required_input_slots": [item.slot_id for item in semantic_inputs if item.required],
                "optional_input_slots": [item.slot_id for item in semantic_inputs if not item.required],
                "produced_output_slots": [item.slot_id for item in semantic_outputs],
                "side_effects": list(definition.side_effects or []),
                "operation_type": definition.operation_type,
                "io_contract_version": TOOL_IO_CONTRACT_VERSION,
            })
        return rows


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
    from .risk import build_risk_tool_definitions

    graph_relation_tools = (
        build_graph_relation_tool_definitions(impact_service)
        if impact_service is not None
        else []
    )
    definitions = [
        *build_evidence_tool_definitions(provider),
        *build_internal_system_tool_definitions(provider),
        *build_graph_context_tool_definitions(provider),
        *build_risk_tool_definitions(),
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
