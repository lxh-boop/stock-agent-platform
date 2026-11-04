from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .contracts import (
    GraphNodeKind,
    GraphRef,
    GraphResolution,
    ResolutionCandidate,
)
from .errors import AmbiguousGraphReferenceError, GraphResolutionError
from .store import FinancialGraphStore, normalize_identity_value


# Code extraction is only a candidate generator. Neo4j remains authoritative.
_SECURITIES = re.compile(r"(?<![\d.])([034689]\d{5})(?:\.(SH|SZ|BJ))?(?!\d)", re.IGNORECASE)
_AMOUNT_SUFFIX = re.compile(r"^\s*(?:元|万|万元|亿|亿元|%|％|人民币|cny|rmb)", re.IGNORECASE)
_QUOTED = re.compile(r"[“\"']([^“”\"']{2,40})[”\"']")


def _text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
    return result


@dataclass(frozen=True)
class ResolutionPolicy:
    graph_id: str = "financial_graph"
    exact_only_for_locked: bool = True
    allow_fulltext_candidates: bool = True
    fulltext_limit: int = 8
    minimum_fulltext_score: float = 0.65
    maximum_refs: int = 20


class GraphEntityIdentityService:
    """Resolve user mentions into stable GraphRefs.

    The service never creates a canonical object from an LLM guess. Unknown and
    ambiguous mentions are returned to the coordinator as missing context.
    """

    def __init__(
        self,
        store: FinancialGraphStore,
        *,
        policy: ResolutionPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or ResolutionPolicy(graph_id=store.graph_id)

    def resolve_identity(
        self,
        value: str,
        *,
        namespace: str = "",
        role: str = "focus",
        locked: bool = True,
        as_of_time: str = "",
    ) -> list[ResolutionCandidate]:
        normalized = normalize_identity_value(value)
        if not normalized:
            return []
        rows = self.store.execute_read(
            """
            MATCH (identity:GraphIdentity)-[:IDENTIFIES]->(object:GraphObject)
            WHERE identity.normalized_value = $value
              AND ($namespace = '' OR identity.namespace = $namespace)
              AND (identity.valid_from = '' OR $as_of = '' OR identity.valid_from <= $as_of)
              AND (identity.valid_to = '' OR $as_of = '' OR identity.valid_to >= $as_of)
              AND object.authority IN ['validated', 'canonical']
            OPTIONAL MATCH (other:GraphIdentity)-[:IDENTIFIES]->(object)
            RETURN object.object_id AS object_id,
                   object.name AS name,
                   object.object_class AS object_class,
                   object.confidence AS object_confidence,
                   collect({namespace: other.namespace, value: other.value, primary: other.is_primary}) AS identities,
                   max(identity.confidence) AS identity_confidence
            ORDER BY identity_confidence DESC, object_confidence DESC
            LIMIT 20
            """,
            {
                "value": normalized,
                "namespace": str(namespace or "").strip().lower(),
                "as_of": str(as_of_time or ""),
            },
        )
        result: list[ResolutionCandidate] = []
        for row in rows:
            confidence = min(
                1.0,
                max(float(row.get("identity_confidence") or 0.0), float(row.get("object_confidence") or 0.0)),
            )
            result.append(
                ResolutionCandidate(
                    graph_ref=GraphRef(
                        graph_id=self.policy.graph_id,
                        node_id=str(row.get("object_id") or ""),
                        node_kind=GraphNodeKind.OBJECT,
                        role=role,
                        as_of_time=as_of_time,
                        source="neo4j_exact_identity",
                        confidence=confidence,
                        locked=locked,
                    ),
                    matched_text=value,
                    matched_by="identity",
                    display_name=str(row.get("name") or ""),
                    identities=[dict(item) for item in row.get("identities") or [] if isinstance(item, dict)],
                )
            )
        return result

    def resolve_name(
        self,
        value: str,
        *,
        role: str = "focus",
        locked: bool = True,
        as_of_time: str = "",
    ) -> list[ResolutionCandidate]:
        text = _text(value)
        if not text:
            return []
        rows = self.store.execute_read(
            """
            MATCH (object:GraphObject)
            WHERE (object.name = $name OR $name IN coalesce(object.aliases, []))
              AND object.authority IN ['validated', 'canonical']
              AND (object.valid_from = '' OR $as_of = '' OR object.valid_from <= $as_of)
              AND (object.valid_to = '' OR $as_of = '' OR object.valid_to >= $as_of)
            OPTIONAL MATCH (identity:GraphIdentity)-[:IDENTIFIES]->(object)
            RETURN object.object_id AS object_id,
                   object.name AS name,
                   object.object_class AS object_class,
                   object.confidence AS confidence,
                   collect({namespace: identity.namespace, value: identity.value, primary: identity.is_primary}) AS identities
            ORDER BY confidence DESC
            LIMIT 20
            """,
            {"name": text, "as_of": str(as_of_time or "")},
        )
        return [
            ResolutionCandidate(
                graph_ref=GraphRef(
                    graph_id=self.policy.graph_id,
                    node_id=str(row.get("object_id") or ""),
                    node_kind=GraphNodeKind.OBJECT,
                    role=role,
                    as_of_time=as_of_time,
                    source="neo4j_exact_name_or_alias",
                    confidence=float(row.get("confidence") or 0.8),
                    locked=locked,
                ),
                matched_text=text,
                matched_by="name_or_alias",
                display_name=str(row.get("name") or ""),
                identities=[dict(item) for item in row.get("identities") or [] if isinstance(item, dict)],
            )
            for row in rows
            if row.get("object_id")
        ]

    def fulltext_candidates(
        self,
        value: str,
        *,
        role: str = "focus",
        as_of_time: str = "",
    ) -> list[ResolutionCandidate]:
        if not self.policy.allow_fulltext_candidates:
            return []
        text = _text(value)
        if len(text) < 2:
            return []
        try:
            rows = self.store.execute_read(
                """
                CALL db.index.fulltext.queryNodes('graph_object_text', $query, {limit: $limit})
                YIELD node, score
                WHERE score >= $minimum_score
                  AND node.authority IN ['validated', 'canonical']
                OPTIONAL MATCH (identity:GraphIdentity)-[:IDENTIFIES]->(node)
                RETURN node.object_id AS object_id,
                       node.name AS name,
                       node.object_class AS object_class,
                       node.confidence AS object_confidence,
                       score,
                       collect({namespace: identity.namespace, value: identity.value, primary: identity.is_primary}) AS identities
                ORDER BY score DESC, object_confidence DESC
                """,
                {
                    "query": text,
                    "limit": int(self.policy.fulltext_limit),
                    "minimum_score": float(self.policy.minimum_fulltext_score),
                },
            )
        except Exception:
            return []
        return [
            ResolutionCandidate(
                graph_ref=GraphRef(
                    graph_id=self.policy.graph_id,
                    node_id=str(row.get("object_id") or ""),
                    node_kind=GraphNodeKind.OBJECT,
                    role=role,
                    as_of_time=as_of_time,
                    source="neo4j_fulltext_candidate",
                    confidence=min(0.79, max(0.0, float(row.get("score") or 0.0))),
                    locked=False,
                ),
                matched_text=text,
                matched_by="fulltext_candidate",
                display_name=str(row.get("name") or ""),
                identities=[dict(item) for item in row.get("identities") or [] if isinstance(item, dict)],
            )
            for row in rows
            if row.get("object_id")
        ]

    @staticmethod
    def extract_candidate_mentions(text: str) -> list[str]:
        raw = str(text or "")
        candidates: list[str] = []
        for match in _SECURITIES.finditer(raw):
            suffix = raw[match.end(): match.end() + 8]
            if _AMOUNT_SUFFIX.match(suffix):
                continue
            code, exchange = match.groups()
            candidates.append(f"{code}.{exchange.upper()}" if exchange else code)
        candidates.extend(item.strip() for item in _QUOTED.findall(raw) if item.strip())
        return _dedupe(candidates)

    def resolve_request(
        self,
        text: str,
        *,
        inherited_refs: list[GraphRef] | None = None,
        role: str = "focus",
        as_of_time: str = "",
        explicit_mentions: list[str] | None = None,
    ) -> GraphResolution:
        mentions = _dedupe([*(explicit_mentions or []), *self.extract_candidate_mentions(text)])
        candidates: list[ResolutionCandidate] = []
        unresolved: list[str] = []
        ambiguous: list[dict[str, Any]] = []
        for mention in mentions:
            exact = self.resolve_identity(
                mention,
                role=role,
                locked=True,
                as_of_time=as_of_time,
            )
            if not exact:
                exact = self.resolve_name(
                    mention,
                    role=role,
                    locked=True,
                    as_of_time=as_of_time,
                )
            unique = {item.graph_ref.node_id: item for item in exact}
            if len(unique) == 1:
                item = next(iter(unique.values()))
                if not any(existing.graph_ref.node_id == item.graph_ref.node_id for existing in candidates):
                    candidates.append(item)
                continue
            if len(unique) > 1:
                ambiguous.append(
                    {
                        "mention": mention,
                        "candidates": [item.to_dict() for item in list(unique.values())[:8]],
                    }
                )
                continue
            fuzzy = self.fulltext_candidates(mention, role=role, as_of_time=as_of_time)
            if len({item.graph_ref.node_id for item in fuzzy}) == 1:
                # Fuzzy results are not locked/authoritative. The coordinator may
                # use them as a clarification suggestion, never as an explicit anchor.
                ambiguous.append({"mention": mention, "candidates": [fuzzy[0].to_dict()]})
            elif fuzzy:
                ambiguous.append({"mention": mention, "candidates": [item.to_dict() for item in fuzzy[:8]]})
            else:
                unresolved.append(mention)

        if not mentions and inherited_refs:
            for ref in inherited_refs[: self.policy.maximum_refs]:
                if not any(item.graph_ref.node_id == ref.node_id for item in candidates):
                    candidates.append(
                        ResolutionCandidate(
                            graph_ref=GraphRef(
                                **{
                                    **ref.to_dict(),
                                    "role": role,
                                    "source": "conversation_graph_ref",
                                    "locked": bool(ref.locked),
                                }
                            ),
                            matched_text="",
                            matched_by="conversation_graph_ref",
                        )
                    )

        return GraphResolution(
            candidates=candidates[: self.policy.maximum_refs],
            unresolved_mentions=unresolved,
            ambiguous_mentions=ambiguous,
        )

    def require_one(self, value: str, *, role: str = "focus", as_of_time: str = "") -> GraphRef:
        result = self.resolve_request(value, role=role, as_of_time=as_of_time, explicit_mentions=[value])
        if result.ambiguous_mentions:
            raise AmbiguousGraphReferenceError(f"ambiguous_graph_reference:{value}")
        if not result.refs:
            raise GraphResolutionError(f"graph_reference_not_found:{value}")
        if len(result.refs) != 1:
            raise AmbiguousGraphReferenceError(f"multiple_graph_references:{value}")
        return result.refs[0]

    def get_identity_value(self, ref: GraphRef, *, namespaces: list[str]) -> str:
        wanted = [str(item).strip().lower() for item in namespaces if str(item).strip()]
        rows = self.store.execute_read(
            """
            MATCH (identity:GraphIdentity)-[:IDENTIFIES]->(object:GraphObject {object_id: $object_id})
            WHERE identity.namespace IN $namespaces
              AND (identity.valid_from = '' OR $as_of = '' OR identity.valid_from <= $as_of)
              AND (identity.valid_to = '' OR $as_of = '' OR identity.valid_to >= $as_of)
            RETURN identity.namespace AS namespace,
                   identity.value AS value,
                   identity.is_primary AS is_primary,
                   identity.confidence AS confidence
            ORDER BY is_primary DESC, confidence DESC
            LIMIT 1
            """,
            {
                "object_id": ref.node_id,
                "namespaces": wanted,
                "as_of": ref.as_of_time,
            },
        )
        return str(rows[0].get("value") or "") if rows else ""
