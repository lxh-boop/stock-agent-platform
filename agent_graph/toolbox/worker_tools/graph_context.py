"""Private database-write tools for W08.

Only non-trading write capabilities are registered here. Trading execution
remains outside W08.
"""

from __future__ import annotations

from typing import Any

from agent_graph.workers.cards import DATABASE_WRITER
from agent_graph.financial_graph.provider_adapter import GraphProviderAdapter
from agent_graph.toolbox import (
    OP_SYSTEM,
    TOOL_VISIBILITY_WORKER_PRIVATE,
    ToolDefinition,
    ToolInputContract,
    ToolOutputContract,
    description,
    result_schema,
    schema,
)


DATABASE_WRITE_PORTFOLIO_GRAPH_CONTEXT = "database.write_portfolio_graph_context"
DATABASE_WRITE_EVIDENCE_GRAPH_CONTEXT = "database.write_evidence_graph_context"


def build_graph_context_tool_definitions(
    provider: GraphProviderAdapter,
) -> list[ToolDefinition]:
    """Bind W08's current non-trading database writes."""

    def write_portfolio_graph_context(
        arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        state = arguments.get("portfolio_state")
        if not isinstance(state, dict):
            raise ValueError("portfolio_state_required")
        return provider.materialize_portfolio_snapshot(
            user_id=str(arguments.get("user_id") or context.get("user_id") or "default"),
            portfolio_state=state,
            as_of_time=str(arguments.get("as_of_time") or ""),
            source_task_id=str(context.get("task_id") or ""),
            source_agent_id=str(context.get("agent_role") or DATABASE_WRITER),
        )

    def write_evidence_graph_context(
        arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        collection = arguments.get("evidence_collection")
        if not isinstance(collection, dict):
            raise ValueError("evidence_collection_required")
        return provider.materialize_evidence_graph(
            evidence_collection=collection,
            source_task_id=str(context.get("task_id") or ""),
            source_agent_id=str(context.get("agent_role") or DATABASE_WRITER),
        )

    return [
        ToolDefinition(
            name=DATABASE_WRITE_PORTFOLIO_GRAPH_CONTEXT,
            display_name="Write Portfolio Graph Context",
            description=description(
                "Write an authoritative portfolio result into the Neo4j database as a portfolio graph context.",
                "W08 receives a portfolio state that must be persisted for graph-relation retrieval.",
                "Reading portfolio state, changing cash or positions, or executing trades.",
                "portfolio_state, user_id, and optional as_of_time.",
                "portfolio_ref, holding_refs, unresolved positions, and write summary.",
            ),
            input_schema=schema(
                {
                    "portfolio_state": {"type": "object"},
                    "user_id": {"type": "string"},
                    "as_of_time": {"type": "string"},
                },
                required=["portfolio_state", "user_id"],
            ),
            output_schema=result_schema([]),
            execution_handler=write_portfolio_graph_context,
            supported_actions=["write_portfolio_graph_context"],
            supported_objects=["portfolio_graph_context"],
            produced_outputs=["portfolio_graph_context"],
            input_contracts=[
                ToolInputContract(slot_id="portfolio_state", required=True),
                ToolInputContract(slot_id="user_id", required=True),
                ToolInputContract(slot_id="as_of_time", required=False),
            ],
            output_contracts=[
                ToolOutputContract(slot_id="portfolio_graph_context", source_path="data"),
            ],
            operation_type=OP_SYSTEM,
            allowed_agent_types=[DATABASE_WRITER],
            permission_scope=OP_SYSTEM,
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            side_effects=["neo4j_write"],
            mutates_business_state=False,
            idempotency="idempotent_upsert",
            audit_level="full",
            tags=["worker_private", "database_write", "neo4j", "portfolio"],
        ),
        ToolDefinition(
            name=DATABASE_WRITE_EVIDENCE_GRAPH_CONTEXT,
            display_name="Write Evidence Graph Context",
            description=description(
                "Write an upstream external-evidence collection into the Neo4j database.",
                "W08 receives evidence that must be persisted for graph-relation retrieval.",
                "Collecting or interpreting evidence, modifying trading state, or executing trades.",
                "evidence_collection and runtime trace metadata.",
                "evidence GraphRefs, ingestion results, and write counts.",
            ),
            input_schema=schema(
                {"evidence_collection": {"type": "object"}},
                required=["evidence_collection"],
            ),
            output_schema=result_schema([]),
            execution_handler=write_evidence_graph_context,
            supported_actions=["write_evidence_graph_context"],
            supported_objects=["evidence_graph_context"],
            produced_outputs=["evidence_graph_context"],
            input_contracts=[
                ToolInputContract(slot_id="evidence_collection", required=True),
            ],
            output_contracts=[
                ToolOutputContract(slot_id="evidence_graph_context", source_path="data"),
            ],
            operation_type=OP_SYSTEM,
            allowed_agent_types=[DATABASE_WRITER],
            permission_scope=OP_SYSTEM,
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            side_effects=["neo4j_write"],
            mutates_business_state=False,
            idempotency="source_ref_upsert",
            audit_level="full",
            tags=["worker_private", "database_write", "neo4j", "evidence"],
        ),
    ]


__all__ = [
    "DATABASE_WRITE_EVIDENCE_GRAPH_CONTEXT",
    "DATABASE_WRITE_PORTFOLIO_GRAPH_CONTEXT",
    "build_graph_context_tool_definitions",
]
