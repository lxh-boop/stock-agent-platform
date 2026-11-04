"""Run-level resources shared by LangGraph engine tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.graph.evidence_ingestion import EvidenceIngestionService
from agent.graph.identity import GraphEntityIdentityService
from agent.graph.impact_service import GraphImpactService
from agent.graph.patch_validator import GraphPatchValidator
from agent.graph.portfolio_graph import PortfolioGraphService
from agent.graph.provider_adapter import GraphProviderAdapter
from agent.graph.settings import Neo4jSettings
from agent.graph.store import Neo4jFinancialGraphStore
from agent.tool_runtime.registry import ToolRegistry
from agent.worker_tools.registry import build_worker_tool_registry


@dataclass
class AgentResources:
    """Graph store + worker tool registry + run context for one agent run.

    Mirrors the authoritative initialization template from
    ``AgentCollaborationCoordinator.__init__`` so both engines observe the same
    Neo4j connectivity, schema and admission behavior.
    """

    store: Neo4jFinancialGraphStore
    provider: GraphProviderAdapter
    impact_service: GraphImpactService
    tool_registry: ToolRegistry
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        user_id: str,
        session_id: str,
        run_id: str,
        output_dir: str | None = None,
        db_path: str | None = None,
        graph_settings: Neo4jSettings | None = None,
    ) -> "AgentResources":
        settings = graph_settings or Neo4jSettings.from_env()
        store = Neo4jFinancialGraphStore(settings)
        store.verify_connectivity()
        store.ensure_schema()
        identity = GraphEntityIdentityService(store)
        validator = GraphPatchValidator(store)
        provider = GraphProviderAdapter(
            identity=identity,
            evidence_ingestion=EvidenceIngestionService(validator),
            portfolio_graph=PortfolioGraphService(identity, validator),
        )
        impact_service = GraphImpactService(store)
        tool_registry = build_worker_tool_registry(provider=provider, impact_service=impact_service)
        context = {
            "user_id": str(user_id or "default"),
            "session_id": str(session_id or ""),
            "run_id": str(run_id or ""),
            "output_dir": str(output_dir or "outputs"),
            "db_path": db_path,
        }
        return cls(
            store=store,
            provider=provider,
            impact_service=impact_service,
            tool_registry=tool_registry,
            context=context,
        )

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001 - closing must never mask the run result
            pass


__all__ = ["AgentResources"]
