"""Private read-only graph-relation tools for W03.

Tool compatibility is driven by declared input slots.  No tool is selected by
counting entities.  The Worker planner sees only tools whose required slots are
already available in the current task context.
"""

from __future__ import annotations

from typing import Any

from agent_graph.workers.cards import GRAPH_RELATION_RETRIEVER
from agent_graph.financial_graph.contracts import GraphRef, refs_from
from agent_graph.financial_graph.impact_service import GraphImpactService
from agent_graph.toolbox import (
    OP_READ,
    TOOL_VISIBILITY_WORKER_PRIVATE,
    ToolDefinition,
    ToolInputContract,
    ToolOutputContract,
    description,
    result_schema,
    schema,
)


GRAPH_RELATION_READ_NEIGHBORHOOD = "graph.relations.read_neighborhood"
GRAPH_RELATION_FIND_PATHS = "graph.relations.find_paths"


def _graph_refs(value: Any) -> list[GraphRef]:
    if isinstance(value, GraphRef):
        return [value]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, GraphRef):
            rows.append(item.to_dict())
        elif isinstance(item, dict):
            rows.append(dict(item))
    return refs_from(rows)


def _relation_payload(
    impact_service: GraphImpactService,
    *,
    paths: list[Any],
    query_basis: str,
    input_refs: dict[str, list[GraphRef]],
) -> dict[str, Any]:
    summary = impact_service.summarize_relations(paths)
    path_rows = [path.to_dict() for path in paths]
    slots = {
        "financial_relation_paths": {
            "query_basis": query_basis,
            "paths": path_rows,
            "path_count": len(path_rows),
        },
        "graph_relation_facts": {
            "query_basis": query_basis,
            "summary": summary,
            "business_empty": not bool(path_rows),
        },
    }
    return {
        "query_basis": query_basis,
        "input_refs": {
            key: [ref.to_dict() for ref in refs]
            for key, refs in input_refs.items()
        },
        "relation_paths": path_rows,
        "relation_summary": summary,
        "slots": slots,
        "produced_information_slots": [
            "financial_relation_paths",
            "graph_relation_facts",
        ],
        "business_empty": not bool(path_rows),
        "write_performed": False,
    }


def build_graph_relation_tool_definitions(
    impact_service: GraphImpactService,
) -> list[ToolDefinition]:
    """Bind W03 private tools to the run-scoped graph service."""

    def read_neighborhood(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        entity_refs = _graph_refs(arguments.get("authoritative_entity_refs"))
        if not entity_refs:
            return {
                "success": False,
                "message": "Authoritative entity refs are required.",
                "data": {},
                "warnings": [],
                "errors": ["missing_required_input:authoritative_entity_refs"],
                "error_type": "missing_required_input",
                "error_message": "authoritative_entity_refs is required.",
                "failure_kind": "tool_input_missing",
                "retryable": False,
            }
        as_of_time = str(arguments.get("as_of_time") or "")
        paths = impact_service.find_neighborhood_paths(
            entity_refs=entity_refs,
            as_of_time=as_of_time,
        )
        data = _relation_payload(
            impact_service,
            paths=paths,
            query_basis="authoritative_entity_refs",
            input_refs={"authoritative_entity_refs": entity_refs},
        )
        return {
            "success": True,
            "message": (
                f"Retrieved {len(paths)} auditable graph relation path(s)."
                if paths
                else "No matching graph relation path was found."
            ),
            "data": data,
            "warnings": [] if paths else ["business_result_empty:no_graph_relation_path"],
            "errors": [],
            "sources": [],
        }

    def find_paths(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        source_refs = _graph_refs(arguments.get("source_entity_refs"))
        target_refs = _graph_refs(arguments.get("target_entity_refs"))
        missing = []
        if not source_refs:
            missing.append("source_entity_refs")
        if not target_refs:
            missing.append("target_entity_refs")
        if missing:
            return {
                "success": False,
                "message": "Explicit source and target entity refs are required.",
                "data": {},
                "warnings": [],
                "errors": [f"missing_required_input:{item}" for item in missing],
                "error_type": "missing_required_input",
                "error_message": ",".join(missing),
                "failure_kind": "tool_input_missing",
                "retryable": False,
            }
        as_of_time = str(arguments.get("as_of_time") or "")
        paths = impact_service.find_relation_paths(
            source_refs=source_refs,
            target_refs=target_refs,
            as_of_time=as_of_time,
        )
        data = _relation_payload(
            impact_service,
            paths=paths,
            query_basis="source_entity_refs+target_entity_refs",
            input_refs={
                "source_entity_refs": source_refs,
                "target_entity_refs": target_refs,
            },
        )
        return {
            "success": True,
            "message": (
                f"Retrieved {len(paths)} auditable graph path(s)."
                if paths
                else "No matching graph path was found."
            ),
            "data": data,
            "warnings": [] if paths else ["business_result_empty:no_graph_relation_path"],
            "errors": [],
            "sources": [],
        }

    ref_array = {
        "type": "array",
        "items": {"type": "object"},
        "minItems": 1,
    }
    common_outputs = ["financial_relation_paths", "graph_relation_facts"]
    return [
        ToolDefinition(
            name=GRAPH_RELATION_READ_NEIGHBORHOOD,
            display_name="Read Graph Neighborhood Relations",
            description=description(
                "Read auditable graph relations around one or more authoritative entity refs.",
                "The authoritative_entity_refs slot is available and the contract needs relation facts or paths.",
                "Inferring source/target roles, creating entities, business interpretation, or graph writes.",
                "authoritative_entity_refs and optional as_of_time.",
                "financial_relation_paths and graph_relation_facts.",
            ),
            input_schema=schema(
                {
                    "authoritative_entity_refs": ref_array,
                    "as_of_time": {"type": "string"},
                },
                required=["authoritative_entity_refs"],
            ),
            output_schema=result_schema(["slots", "produced_information_slots"]),
            execution_handler=read_neighborhood,
            supported_actions=["read_neighborhood_relations"],
            supported_objects=["financial_graph_ref"],
            produced_outputs=common_outputs,
            required_input_slots=["authoritative_entity_refs"],
            optional_input_slots=["as_of_time"],
            input_contracts=[
                ToolInputContract(slot_id="authoritative_entity_refs", required=True),
                ToolInputContract(slot_id="as_of_time", required=False),
            ],
            output_contracts=[
                ToolOutputContract(
                    slot_id="financial_relation_paths",
                    source_path="data.slots.financial_relation_paths",
                ),
                ToolOutputContract(
                    slot_id="graph_relation_facts",
                    source_path="data.slots.graph_relation_facts",
                ),
            ],
            operation_type=OP_READ,
            allowed_agent_types=[GRAPH_RELATION_RETRIEVER],
            permission_scope=OP_READ,
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            side_effects=[],
            mutates_business_state=False,
            idempotency="read_only",
            audit_level="full",
            tags=["worker_private", "graph_relation", "slot_compatible", "read_only"],
        ),
        ToolDefinition(
            name=GRAPH_RELATION_FIND_PATHS,
            display_name="Find Graph Paths Between Roles",
            description=description(
                "Read auditable paths between explicitly role-bound source and target entity refs.",
                "Both source_entity_refs and target_entity_refs slots are available and the contract needs relation paths.",
                "Guessing endpoint roles from entity count, creating entities, business interpretation, or graph writes.",
                "source_entity_refs, target_entity_refs and optional as_of_time.",
                "financial_relation_paths and graph_relation_facts.",
            ),
            input_schema=schema(
                {
                    "source_entity_refs": ref_array,
                    "target_entity_refs": ref_array,
                    "as_of_time": {"type": "string"},
                },
                required=["source_entity_refs", "target_entity_refs"],
            ),
            output_schema=result_schema(["slots", "produced_information_slots"]),
            execution_handler=find_paths,
            supported_actions=["find_relation_paths"],
            supported_objects=["financial_graph_ref"],
            produced_outputs=common_outputs,
            required_input_slots=["source_entity_refs", "target_entity_refs"],
            optional_input_slots=["as_of_time"],
            input_contracts=[
                ToolInputContract(slot_id="source_entity_refs", required=True),
                ToolInputContract(slot_id="target_entity_refs", required=True),
                ToolInputContract(slot_id="as_of_time", required=False),
            ],
            output_contracts=[
                ToolOutputContract(
                    slot_id="financial_relation_paths",
                    source_path="data.slots.financial_relation_paths",
                ),
                ToolOutputContract(
                    slot_id="graph_relation_facts",
                    source_path="data.slots.graph_relation_facts",
                ),
            ],
            operation_type=OP_READ,
            allowed_agent_types=[GRAPH_RELATION_RETRIEVER],
            permission_scope=OP_READ,
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            side_effects=[],
            mutates_business_state=False,
            idempotency="read_only",
            audit_level="full",
            tags=["worker_private", "graph_relation", "slot_compatible", "read_only"],
        ),
    ]


__all__ = [
    "GRAPH_RELATION_FIND_PATHS",
    "GRAPH_RELATION_READ_NEIGHBORHOOD",
    "build_graph_relation_tool_definitions",
]
