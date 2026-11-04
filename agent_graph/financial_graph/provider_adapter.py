"""Stable GraphRef provider facade used by the collaboration runtime.

The facade preserves the existing constructor and method signatures while
delegating evidence, portfolio, and risk work to domain adapters. Provider
identifiers and service-specific payloads remain private behind this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import GraphRef
from .evidence_ingestion import EvidenceIngestionService
from .identity import GraphEntityIdentityService
from .portfolio_graph import PortfolioGraphService
from .providers import (
    EvidenceGraphProvider,
    PortfolioGraphProvider,
    PortfolioRiskProvider,
    ProviderIdentityResolver,
)


@dataclass
class GraphProviderAdapter:
    """Compatibility facade for domain-scoped GraphRef provider adapters.

    Public Agents continue to depend on this stable facade. Provider identifiers,
    existing service payloads, and graph persistence remain private behind the
    domain adapters.
    """

    identity: GraphEntityIdentityService
    evidence_ingestion: EvidenceIngestionService
    portfolio_graph: PortfolioGraphService

    def __post_init__(self) -> None:
        self._identity_resolver = ProviderIdentityResolver(self.identity)
        self._evidence_provider = EvidenceGraphProvider(
            identity_resolver=self._identity_resolver,
            evidence_ingestion=self.evidence_ingestion,
        )
        self._portfolio_provider = PortfolioGraphProvider(self.portfolio_graph)
        self._risk_provider = PortfolioRiskProvider()

    def provider_symbol(self, ref: GraphRef) -> str:
        return self._identity_resolver.provider_symbol(ref)

    def public_entity_descriptor(self, ref: GraphRef) -> dict[str, Any]:
        """Return a user-facing identity descriptor resolved by graph rules.

        The descriptor is safe for a report Worker: it contains only the public
        code, display label, exchange, GraphRef, and provenance. Provider-private
        identifiers remain behind the graph boundary. Missing labels stay empty
        and must never be guessed by an LLM.
        """

        # Canonical graph object IDs already encode the public exchange and code
        # (for example ``cn:security:sse:601899``). Parse those deterministic
        # fields first and query Neo4j only for the human-readable label. This
        # avoids repeated identity reads while keeping the graph identity table
        # authoritative for names and aliases.
        node_parts = str(ref.node_id or "").split(":")
        candidate = node_parts[-1] if node_parts else ""
        public_code = candidate if candidate.isdigit() and len(candidate) == 6 else ""
        exchange = node_parts[-2].upper() if len(node_parts) >= 2 else ""
        display_label = self.identity.get_identity_value(
            ref, namespaces=["display_name"]
        )
        if not public_code:
            public_code = self.identity.get_identity_value(
                ref, namespaces=["symbol", "exchange_symbol"]
            )
            public_code = str(public_code or "").split(".", 1)[0]
        return {
            "entity_ref": ref.to_dict(),
            "public_code": str(public_code or ""),
            "display_label": str(display_label or ""),
            "exchange": exchange,
            "identity_source": "graph_identity",
            "identity_locked": bool(ref.locked),
        }

    def materialize_evidence_graph(
        self,
        *,
        evidence_collection: dict[str, Any],
        source_task_id: str,
        source_agent_id: str,
    ) -> dict[str, Any]:
        return self._evidence_provider.materialize_evidence_graph(
            evidence_collection=evidence_collection,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
        )

    def read_portfolio_state(
        self,
        *,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        return self._portfolio_provider.read_portfolio_state(
            user_id=user_id,
            output_dir=output_dir,
            db_path=db_path,
        )

    def materialize_portfolio_snapshot(
        self,
        *,
        user_id: str,
        portfolio_state: dict[str, Any],
        as_of_time: str,
        source_task_id: str,
        source_agent_id: str,
    ) -> dict[str, Any]:
        return self._portfolio_provider.materialize_portfolio_snapshot(
            user_id=user_id,
            portfolio_state=portfolio_state,
            as_of_time=as_of_time,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
        )

    def load_portfolio_snapshot(
        self,
        *,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
        as_of_time: str,
        source_task_id: str,
        source_agent_id: str,
    ) -> dict[str, Any]:
        return self._portfolio_provider.load_portfolio_snapshot(
            user_id=user_id,
            output_dir=output_dir,
            db_path=db_path,
            as_of_time=as_of_time,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
        )

    def analyze_risk(
        self,
        *,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
        portfolio_ref: GraphRef | None = None,
    ) -> dict[str, Any]:
        return self._risk_provider.analyze_risk(
            user_id=user_id,
            output_dir=output_dir,
            db_path=db_path,
            portfolio_ref=portfolio_ref,
        )


__all__ = ["GraphProviderAdapter"]
