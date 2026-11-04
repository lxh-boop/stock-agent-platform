"""Evidence-domain bridge from GraphRefs to external evidence services.

Collection and persistence are deliberately separated. W01 uses the read-only
collection path. W08 may later persist a supplied collection into the financial
graph through the explicit write method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_graph.financial_graph.contracts import GraphRef
from agent_graph.financial_graph.evidence_ingestion import EvidenceIngestionService, ExtractedMention

from .common import ProviderIdentityResolver


@dataclass
class EvidenceGraphProvider:
    """Evidence operations behind the GraphRef boundary."""

    identity_resolver: ProviderIdentityResolver
    evidence_ingestion: EvidenceIngestionService

    def materialize_evidence_graph(
        self,
        *,
        evidence_collection: dict[str, Any],
        source_task_id: str,
        source_agent_id: str,
    ) -> dict[str, Any]:
        """Persist a supplied evidence collection as derived Neo4j graph state."""
        evidence_refs: list[dict[str, Any]] = []
        ingestion_results: list[dict[str, Any]] = []
        for result in evidence_collection.get("results") or []:
            if not isinstance(result, dict) or not isinstance(result.get("focus_ref"), dict):
                continue
            focus_ref = GraphRef.from_dict(result["focus_ref"])
            code = self.identity_resolver.provider_symbol(focus_ref)
            for index, row in enumerate(result.get("records") or [], start=1):
                if not isinstance(row, dict):
                    continue
                evidence_class = str(
                    row.get("evidence_type") or row.get("source_type") or "news"
                )
                try:
                    applied = self.evidence_ingestion.ingest(
                        record=row,
                        evidence_class=evidence_class,
                        source_task_id=source_task_id,
                        source_agent_id=source_agent_id,
                        mentions=[
                            ExtractedMention(
                                mention_text=str(
                                    row.get("stock_name") or row.get("name") or code
                                ),
                                resolved_ref=focus_ref,
                                role="about",
                                confidence=1.0,
                            )
                        ],
                        assertions=[],
                        source_ref=str(
                            row.get("source_id")
                            or row.get("news_id")
                            or f"provider:{code}:{index}"
                        ),
                    )
                    ingestion_results.append(applied)
                    evidence_refs.extend(
                        item
                        for item in applied.get("evidence_refs") or []
                        if isinstance(item, dict)
                    )
                except Exception as exc:  # retain partial writes and explicit failures
                    ingestion_results.append(
                        {
                            "error": f"{type(exc).__name__}:{exc}",
                            "record_index": index,
                            "focus_ref": focus_ref.to_dict(),
                        }
                    )
        errors = [item for item in ingestion_results if item.get("error")]
        return {
            "success": bool(ingestion_results) and len(errors) < len(ingestion_results),
            "evidence_refs": evidence_refs,
            "ingestion_results": ingestion_results,
            "written_record_count": len(ingestion_results) - len(errors),
            "failed_record_count": len(errors),
        }
