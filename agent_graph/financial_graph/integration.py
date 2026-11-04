from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .evidence_ingestion import EvidenceIngestionService
from .identity import GraphEntityIdentityService
from .impact_service import GraphImpactService
from .patch_validator import GraphPatchValidator
from .portfolio_graph import PortfolioGraphService
from .provider_adapter import GraphProviderAdapter
from .settings import Neo4jSettings
from .store import Neo4jFinancialGraphStore
from .view_builder import TaskGraphViewBuilder


@dataclass
class FinancialGraphRuntime:
    store: Neo4jFinancialGraphStore
    identity: GraphEntityIdentityService
    validator: GraphPatchValidator
    evidence: EvidenceIngestionService
    portfolio: PortfolioGraphService
    impact: GraphImpactService
    view_builder: TaskGraphViewBuilder
    provider: GraphProviderAdapter

    def close(self) -> None:
        self.store.close()


@contextmanager
def open_financial_graph_runtime(*, verify: bool = True, ensure_schema: bool = False) -> Iterator[FinancialGraphRuntime]:
    settings = Neo4jSettings.from_env()
    store = Neo4jFinancialGraphStore(settings)
    try:
        if verify:
            store.verify_connectivity()
        if ensure_schema:
            store.ensure_schema()
        identity = GraphEntityIdentityService(store)
        validator = GraphPatchValidator(store)
        evidence = EvidenceIngestionService(validator)
        portfolio = PortfolioGraphService(identity, validator)
        impact = GraphImpactService(store)
        view_builder = TaskGraphViewBuilder(store)
        provider = GraphProviderAdapter(identity, evidence, portfolio)
        yield FinancialGraphRuntime(
            store=store,
            identity=identity,
            validator=validator,
            evidence=evidence,
            portfolio=portfolio,
            impact=impact,
            view_builder=view_builder,
            provider=provider,
        )
    finally:
        store.close()


def financial_graph_health(*, ensure_schema: bool = False) -> dict[str, Any]:
    settings = Neo4jSettings.from_env()
    try:
        with open_financial_graph_runtime(verify=True, ensure_schema=ensure_schema) as runtime:
            rows = runtime.store.execute_read(
                "MATCH (n) RETURN count(n) AS node_count"
            )
            return {
                "success": True,
                "status": "available",
                "settings": settings.public_dict(),
                "node_count": int((rows[0] if rows else {}).get("node_count") or 0),
            }
    except Exception as exc:
        return {
            "success": False,
            "status": "unavailable",
            "settings": settings.public_dict(),
            "error": f"{type(exc).__name__}:{exc}",
        }


def sync_portfolio_payload(
    *,
    user_id: str,
    portfolio_payload: dict[str, Any],
    as_of_time: str,
    source_task_id: str,
    source_agent_id: str = "PORTFOLIO_PIPELINE",
) -> dict[str, Any]:
    with open_financial_graph_runtime(verify=True, ensure_schema=False) as runtime:
        ref, result = runtime.portfolio.upsert_snapshot(
            user_id=str(user_id or "default"),
            portfolio_payload=dict(portfolio_payload or {}),
            as_of_time=str(as_of_time or ""),
            source_task_id=str(source_task_id or "portfolio_sync"),
            source_agent_id=str(source_agent_id or "PORTFOLIO_PIPELINE"),
        )
        unresolved = list(result.get("unresolved_positions") or [])
        return {
            "success": not unresolved,
            "status": "completed" if not unresolved else "partial",
            "portfolio_ref": ref.to_dict(),
            "holding_refs": list(result.get("holding_refs") or []),
            "unresolved_positions": unresolved,
            "graph_write": dict(result.get("applied") or {}),
        }
