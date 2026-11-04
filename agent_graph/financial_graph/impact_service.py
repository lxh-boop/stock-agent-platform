from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .contracts import GraphNodeKind, GraphPathRef, GraphRef, new_graph_id
from .store import FinancialGraphStore


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


@dataclass(frozen=True)
class ImpactQueryPolicy:
    maximum_object_hops: int = 4
    maximum_paths_per_holding: int = 5
    allowed_authorities: tuple[str, ...] = ("canonical", "validated", "candidate")
    holding_predicates: tuple[str, ...] = (
        "predicate:HOLDS",
        "predicate:INDIRECTLY_HOLDS",
        "predicate:TARGETS",
    )


class GraphImpactService:
    """Find evidence/event-to-portfolio paths without deciding market direction.

    Path discovery and financial interpretation stay separate. The returned path
    is auditable and can be handed to a financial-analysis Worker.
    """

    def __init__(self, store: FinancialGraphStore, *, policy: ImpactQueryPolicy | None = None) -> None:
        self.store = store
        self.policy = policy or ImpactQueryPolicy()

    def find_paths(
        self,
        *,
        cause_refs: list[GraphRef],
        portfolio_ref: GraphRef,
        as_of_time: str = "",
    ) -> list[GraphPathRef]:
        if portfolio_ref.node_kind != GraphNodeKind.OBJECT:
            raise ValueError("portfolio_ref_must_be_graph_object")
        cause_object_ids = [ref.node_id for ref in cause_refs if ref.node_kind == GraphNodeKind.OBJECT]
        cause_evidence_ids = [ref.node_id for ref in cause_refs if ref.node_kind == GraphNodeKind.EVIDENCE]
        cause_assertion_ids = [ref.node_id for ref in cause_refs if ref.node_kind == GraphNodeKind.ASSERTION]
        max_relationship_depth = max(2, min(self.policy.maximum_object_hops * 2, 10))
        rows = self.store.execute_read(
            f"""
            MATCH (portfolio:GraphObject {{object_id: $portfolio_id}})
            MATCH (holding_assertion:GraphAssertion)-[:SUBJECT]->(portfolio)
            WHERE holding_assertion.predicate_term_id IN $holding_predicates
              AND holding_assertion.authority IN $authorities
              AND (holding_assertion.valid_from = '' OR $as_of = '' OR holding_assertion.valid_from <= $as_of)
              AND (holding_assertion.valid_to = '' OR $as_of = '' OR holding_assertion.valid_to >= $as_of)
            MATCH (holding_assertion)-[:OBJECT]->(holding:GraphObject)
            MATCH (cause)
            WHERE (cause:GraphObject AND cause.object_id IN $cause_object_ids)
               OR (cause:GraphEvidence AND cause.evidence_id IN $cause_evidence_ids)
               OR (cause:GraphAssertion AND cause.assertion_id IN $cause_assertion_ids)
            MATCH path = shortestPath((cause)-[:SUBJECT|OBJECT|SUPPORTED_BY|CONTRADICTED_BY*..{max_relationship_depth}]-(holding))
            WITH holding, holding_assertion, cause, path,
                 [n IN nodes(path) WHERE n:GraphAssertion | n.assertion_id] AS assertion_ids,
                 [n IN nodes(path) WHERE n:GraphObject | n.object_id] AS object_ids,
                 [n IN nodes(path) WHERE n:GraphEvidence | n.evidence_id] AS evidence_ids,
                 [n IN nodes(path) WHERE n:GraphAssertion | coalesce(n.confidence, 0.5)] AS assertion_confidences
            WHERE all(id IN assertion_ids WHERE id IS NOT NULL)
            RETURN holding.object_id AS holding_id,
                   holding.name AS holding_name,
                   holding.object_class AS holding_class,
                   holding_assertion.properties_json AS holding_properties_json,
                   assertion_ids,
                   object_ids,
                   evidence_ids,
                   assertion_confidences,
                   length(path) AS relationship_length,
                   CASE WHEN cause:GraphEvidence THEN cause.evidence_id
                        WHEN cause:GraphAssertion THEN cause.assertion_id
                        ELSE cause.object_id END AS cause_id,
                   CASE WHEN cause:GraphEvidence THEN 'evidence'
                        WHEN cause:GraphAssertion THEN 'assertion'
                        ELSE 'object' END AS cause_kind
            ORDER BY relationship_length ASC
            LIMIT 500
            """,
            {
                "portfolio_id": portfolio_ref.node_id,
                "holding_predicates": list(self.policy.holding_predicates),
                "authorities": list(self.policy.allowed_authorities),
                "as_of": str(as_of_time or portfolio_ref.as_of_time or ""),
                "cause_object_ids": cause_object_ids,
                "cause_evidence_ids": cause_evidence_ids,
                "cause_assertion_ids": cause_assertion_ids,
            },
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("holding_id") or ""), []).append(row)
        paths: list[GraphPathRef] = []
        for holding_id, holding_rows in grouped.items():
            if not holding_id:
                continue
            for row in holding_rows[: self.policy.maximum_paths_per_holding]:
                confidences = [float(item or 0.0) for item in row.get("assertion_confidences") or []]
                path_confidence = min(confidences) if confidences else 0.4
                distance_penalty = 1.0 / max(1.0, float(row.get("relationship_length") or 1) / 2.0)
                confidence = max(0.0, min(1.0, path_confidence * distance_penalty))
                cause_ref = GraphRef(
                    graph_id=self.store.graph_id,
                    node_id=str(row.get("cause_id") or ""),
                    node_kind=str(row.get("cause_kind") or "object"),
                    role="cause",
                    as_of_time=as_of_time,
                    source="neo4j_impact_path",
                    confidence=confidence,
                    locked=True,
                )
                holding_ref = GraphRef(
                    graph_id=self.store.graph_id,
                    node_id=holding_id,
                    node_kind=GraphNodeKind.OBJECT,
                    role="impacted_holding",
                    as_of_time=as_of_time,
                    source="neo4j_portfolio_holding",
                    confidence=1.0,
                    locked=True,
                )
                props = _decode(row.get("holding_properties_json"))
                explanation = (
                    f"{row.get('cause_id')} reaches holding {row.get('holding_name') or holding_id} "
                    f"through {len(row.get('assertion_ids') or [])} assertion(s)."
                )
                if isinstance(props, dict) and props:
                    explanation += f" Holding properties: {props}."
                paths.append(
                    GraphPathRef(
                        path_id=new_graph_id("impact_path"),
                        start_ref=cause_ref,
                        end_ref=holding_ref,
                        assertion_ids=[str(item) for item in row.get("assertion_ids") or [] if item],
                        object_ids=[str(item) for item in row.get("object_ids") or [] if item],
                        evidence_ids=[str(item) for item in row.get("evidence_ids") or [] if item],
                        path_type="news_or_event_to_portfolio_holding",
                        confidence=confidence,
                        explanation=explanation,
                    )
                )
        return paths


    def find_neighborhood_paths(
        self,
        *,
        entity_refs: list[GraphRef],
        as_of_time: str = "",
    ) -> list[GraphPathRef]:
        """Return auditable relations around all supplied authoritative refs.

        This operation is selected because the ``authoritative_entity_refs``
        input slot is available. It does not branch on entity count: one or
        many refs are processed by the same slot-compatible graph query.
        """

        object_ids = [ref.node_id for ref in entity_refs if ref.node_kind == GraphNodeKind.OBJECT]
        evidence_ids = [ref.node_id for ref in entity_refs if ref.node_kind == GraphNodeKind.EVIDENCE]
        assertion_ids_input = [ref.node_id for ref in entity_refs if ref.node_kind == GraphNodeKind.ASSERTION]
        if not (object_ids or evidence_ids or assertion_ids_input):
            return []
        max_depth = max(1, min(self.policy.maximum_object_hops * 2, 8))
        rows = self.store.execute_read(
            f"""
            MATCH (focus)
            WHERE (focus:GraphObject AND focus.object_id IN $object_ids)
               OR (focus:GraphEvidence AND focus.evidence_id IN $evidence_ids)
               OR (focus:GraphAssertion AND focus.assertion_id IN $assertion_ids)
            MATCH path = (focus)-[:SUBJECT|OBJECT|SUPPORTED_BY|CONTRADICTED_BY*1..{max_depth}]-(neighbor)
            WHERE focus <> neighbor
            WITH focus, neighbor, path,
                 [n IN nodes(path) WHERE n:GraphAssertion | n.assertion_id] AS path_assertion_ids,
                 [n IN nodes(path) WHERE n:GraphObject | n.object_id] AS path_object_ids,
                 [n IN nodes(path) WHERE n:GraphEvidence | n.evidence_id] AS path_evidence_ids,
                 [n IN nodes(path) WHERE n:GraphAssertion | coalesce(n.confidence, 0.5)] AS confidences
            RETURN CASE WHEN focus:GraphEvidence THEN focus.evidence_id
                        WHEN focus:GraphAssertion THEN focus.assertion_id
                        ELSE focus.object_id END AS source_id,
                   CASE WHEN focus:GraphEvidence THEN 'evidence'
                        WHEN focus:GraphAssertion THEN 'assertion'
                        ELSE 'object' END AS source_kind,
                   CASE WHEN neighbor:GraphEvidence THEN neighbor.evidence_id
                        WHEN neighbor:GraphAssertion THEN neighbor.assertion_id
                        ELSE neighbor.object_id END AS target_id,
                   CASE WHEN neighbor:GraphEvidence THEN 'evidence'
                        WHEN neighbor:GraphAssertion THEN 'assertion'
                        ELSE 'object' END AS target_kind,
                   path_assertion_ids, path_object_ids, path_evidence_ids, confidences, length(path) AS relationship_length
            ORDER BY relationship_length ASC
            LIMIT 200
            """,
            {
                "object_ids": object_ids,
                "evidence_ids": evidence_ids,
                "assertion_ids": assertion_ids_input,
                "as_of": str(as_of_time or ""),
            },
        )
        paths: list[GraphPathRef] = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        for row in rows:
            confidences = [float(item or 0.0) for item in row.get("confidences") or []]
            base = min(confidences) if confidences else 0.4
            distance = max(1.0, float(row.get("relationship_length") or 1))
            confidence = max(0.0, min(1.0, base / distance))
            source_id = str(row.get("source_id") or "")
            target_id = str(row.get("target_id") or "")
            assertion_ids = tuple(str(item) for item in row.get("path_assertion_ids") or [] if item)
            signature = (source_id, target_id, assertion_ids)
            if not source_id or not target_id or signature in seen:
                continue
            seen.add(signature)
            start_ref = GraphRef(
                graph_id=self.store.graph_id,
                node_id=source_id,
                node_kind=str(row.get("source_kind") or "object"),
                role="relation_source",
                as_of_time=as_of_time,
                source="neo4j_neighborhood_path",
                confidence=confidence,
                locked=True,
            )
            end_ref = GraphRef(
                graph_id=self.store.graph_id,
                node_id=target_id,
                node_kind=str(row.get("target_kind") or "object"),
                role="relation_neighbor",
                as_of_time=as_of_time,
                source="neo4j_neighborhood_path",
                confidence=confidence,
                locked=True,
            )
            paths.append(
                GraphPathRef(
                    path_id=new_graph_id("neighborhood_path"),
                    start_ref=start_ref,
                    end_ref=end_ref,
                    assertion_ids=list(assertion_ids),
                    object_ids=[str(item) for item in row.get("path_object_ids") or [] if item],
                    evidence_ids=[str(item) for item in row.get("path_evidence_ids") or [] if item],
                    path_type="financial_entity_neighborhood",
                    confidence=confidence,
                    explanation=(
                        f"{start_ref.node_id} is connected to {end_ref.node_id} "
                        "through an auditable financial-graph path."
                    ),
                )
            )
        return paths


    def find_relation_paths(
        self,
        *,
        source_refs: list[GraphRef],
        target_refs: list[GraphRef],
        as_of_time: str = "",
    ) -> list[GraphPathRef]:
        """Find auditable graph relations without interpreting business impact."""
        source_object_ids = [ref.node_id for ref in source_refs if ref.node_kind == GraphNodeKind.OBJECT]
        source_evidence_ids = [ref.node_id for ref in source_refs if ref.node_kind == GraphNodeKind.EVIDENCE]
        source_assertion_ids = [ref.node_id for ref in source_refs if ref.node_kind == GraphNodeKind.ASSERTION]
        target_object_ids = [ref.node_id for ref in target_refs if ref.node_kind == GraphNodeKind.OBJECT]
        target_evidence_ids = [ref.node_id for ref in target_refs if ref.node_kind == GraphNodeKind.EVIDENCE]
        target_assertion_ids = [ref.node_id for ref in target_refs if ref.node_kind == GraphNodeKind.ASSERTION]
        max_depth = max(2, min(self.policy.maximum_object_hops * 2, 10))
        rows = self.store.execute_read(
            f"""
            MATCH (source), (target)
            WHERE ((source:GraphObject AND source.object_id IN $source_object_ids)
                OR (source:GraphEvidence AND source.evidence_id IN $source_evidence_ids)
                OR (source:GraphAssertion AND source.assertion_id IN $source_assertion_ids))
              AND ((target:GraphObject AND target.object_id IN $target_object_ids)
                OR (target:GraphEvidence AND target.evidence_id IN $target_evidence_ids)
                OR (target:GraphAssertion AND target.assertion_id IN $target_assertion_ids))
              AND source <> target
            MATCH path = shortestPath((source)-[:SUBJECT|OBJECT|SUPPORTED_BY|CONTRADICTED_BY*..{max_depth}]-(target))
            WITH source, target, path,
                 [n IN nodes(path) WHERE n:GraphAssertion | n.assertion_id] AS assertion_ids,
                 [n IN nodes(path) WHERE n:GraphObject | n.object_id] AS object_ids,
                 [n IN nodes(path) WHERE n:GraphEvidence | n.evidence_id] AS evidence_ids,
                 [n IN nodes(path) WHERE n:GraphAssertion | coalesce(n.confidence, 0.5)] AS assertion_confidences
            RETURN CASE WHEN source:GraphEvidence THEN source.evidence_id
                        WHEN source:GraphAssertion THEN source.assertion_id
                        ELSE source.object_id END AS source_id,
                   CASE WHEN source:GraphEvidence THEN 'evidence'
                        WHEN source:GraphAssertion THEN 'assertion'
                        ELSE 'object' END AS source_kind,
                   CASE WHEN target:GraphEvidence THEN target.evidence_id
                        WHEN target:GraphAssertion THEN target.assertion_id
                        ELSE target.object_id END AS target_id,
                   CASE WHEN target:GraphEvidence THEN 'evidence'
                        WHEN target:GraphAssertion THEN 'assertion'
                        ELSE 'object' END AS target_kind,
                   assertion_ids, object_ids, evidence_ids, assertion_confidences,
                   length(path) AS relationship_length
            ORDER BY relationship_length ASC
            LIMIT 500
            """,
            {
                "source_object_ids": source_object_ids,
                "source_evidence_ids": source_evidence_ids,
                "source_assertion_ids": source_assertion_ids,
                "target_object_ids": target_object_ids,
                "target_evidence_ids": target_evidence_ids,
                "target_assertion_ids": target_assertion_ids,
                "as_of": str(as_of_time or ""),
            },
        )
        paths: list[GraphPathRef] = []
        for row in rows:
            confidences = [float(item or 0.0) for item in row.get("assertion_confidences") or []]
            base_confidence = min(confidences) if confidences else 0.4
            distance_penalty = 1.0 / max(1.0, float(row.get("relationship_length") or 1) / 2.0)
            confidence = max(0.0, min(1.0, base_confidence * distance_penalty))
            start_ref = GraphRef(
                graph_id=self.store.graph_id,
                node_id=str(row.get("source_id") or ""),
                node_kind=str(row.get("source_kind") or "object"),
                role="relation_source",
                as_of_time=as_of_time,
                source="neo4j_relation_path",
                confidence=confidence,
                locked=True,
            )
            end_ref = GraphRef(
                graph_id=self.store.graph_id,
                node_id=str(row.get("target_id") or ""),
                node_kind=str(row.get("target_kind") or "object"),
                role="relation_target",
                as_of_time=as_of_time,
                source="neo4j_relation_path",
                confidence=confidence,
                locked=True,
            )
            paths.append(
                GraphPathRef(
                    path_id=new_graph_id("relation_path"),
                    start_ref=start_ref,
                    end_ref=end_ref,
                    assertion_ids=[str(item) for item in row.get("assertion_ids") or [] if item],
                    object_ids=[str(item) for item in row.get("object_ids") or [] if item],
                    evidence_ids=[str(item) for item in row.get("evidence_ids") or [] if item],
                    path_type="financial_relation",
                    confidence=confidence,
                    explanation=(
                        f"A graph relation exists between {start_ref.node_id} and "
                        f"{end_ref.node_id} through {len(row.get('assertion_ids') or [])} assertion(s)."
                    ),
                )
            )
        return paths

    def summarize_relations(self, paths: list[GraphPathRef]) -> dict[str, Any]:
        targets: dict[str, dict[str, Any]] = {}
        for path in paths:
            item = targets.setdefault(
                path.end_ref.node_id,
                {
                    "target_ref": path.end_ref.to_dict(),
                    "path_count": 0,
                    "maximum_path_confidence": 0.0,
                },
            )
            item["path_count"] += 1
            item["maximum_path_confidence"] = max(
                item["maximum_path_confidence"], path.confidence
            )
        return {
            "target_count": len(targets),
            "path_count": len(paths),
            "targets": list(targets.values()),
        }

    def summarize_paths(self, paths: list[GraphPathRef]) -> dict[str, Any]:
        holdings: dict[str, dict[str, Any]] = {}
        for path in paths:
            item = holdings.setdefault(
                path.end_ref.node_id,
                {
                    "holding_ref": path.end_ref.to_dict(),
                    "path_count": 0,
                    "maximum_path_confidence": 0.0,
                    "path_refs": [],
                },
            )
            item["path_count"] += 1
            item["maximum_path_confidence"] = max(item["maximum_path_confidence"], path.confidence)
            item["path_refs"].append(path.to_dict())
        return {
            "holding_count": len(holdings),
            "path_count": len(paths),
            "holdings": list(holdings.values()),
        }
