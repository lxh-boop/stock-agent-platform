from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .contracts import GraphNodeKind, GraphRef, TaskGraphView, new_graph_id
from .store import FinancialGraphStore


def _decode_json(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


@dataclass(frozen=True)
class GraphViewPolicy:
    max_depth: int = 3
    max_objects: int = 120
    max_assertions: int = 240
    max_evidence: int = 80
    authorities: tuple[str, ...] = ("validated", "canonical", "candidate")
    include_claims: bool = True
    include_contradicted: bool = True


class TaskGraphViewBuilder:
    """Build a bounded local graph for one Worker task.

    This is a retrieval result placed in working memory; it is not a third
    persistent graph database.
    """

    def __init__(self, store: FinancialGraphStore, *, policy: GraphViewPolicy | None = None) -> None:
        self.store = store
        self.policy = policy or GraphViewPolicy()

    def build(
        self,
        anchors: list[GraphRef],
        *,
        as_of_time: str = "",
        max_depth: int | None = None,
        required_predicates: list[str] | None = None,
    ) -> TaskGraphView:
        refs = [ref for ref in anchors if ref.node_kind in {GraphNodeKind.OBJECT, GraphNodeKind.EVIDENCE, GraphNodeKind.ASSERTION}]
        if not refs:
            return TaskGraphView(
                view_id=new_graph_id("graph_view"),
                anchor_refs=[],
                as_of_time=as_of_time,
                query_policy=self._policy_dict(max_depth=max_depth, required_predicates=required_predicates),
            )
        object_ids = [ref.node_id for ref in refs if ref.node_kind == GraphNodeKind.OBJECT]
        evidence_ids = [ref.node_id for ref in refs if ref.node_kind == GraphNodeKind.EVIDENCE]
        assertion_ids = [ref.node_id for ref in refs if ref.node_kind == GraphNodeKind.ASSERTION]
        depth = max(1, min(int(max_depth or self.policy.max_depth), 5))
        rows = self.store.execute_read(
            f"""
            MATCH (anchor)
            WHERE (anchor:GraphObject AND anchor.object_id IN $object_ids)
               OR (anchor:GraphEvidence AND anchor.evidence_id IN $evidence_ids)
               OR (anchor:GraphAssertion AND anchor.assertion_id IN $assertion_ids)
            OPTIONAL MATCH p=(anchor)-[:SUBJECT|OBJECT|PREDICATE|SUPPORTED_BY|CONTRADICTED_BY|IDENTIFIES|INSTANCE_OF|SUBCLASS_OF*0..{depth}]-(node)
            WITH collect(DISTINCT anchor) + collect(DISTINCT node) AS all_nodes
            UNWIND all_nodes AS n
            WITH DISTINCT n
            WHERE n IS NOT NULL
            RETURN labels(n) AS labels, properties(n) AS props
            LIMIT $node_limit
            """,
            {
                "object_ids": object_ids,
                "evidence_ids": evidence_ids,
                "assertion_ids": assertion_ids,
                "node_limit": self.policy.max_objects + self.policy.max_assertions + self.policy.max_evidence + 100,
            },
        )
        nodes: list[dict[str, Any]] = []
        assertions: list[dict[str, Any]] = []
        evidence: list[dict[str, Any]] = []
        allowed_predicates = set(required_predicates or [])
        for row in rows:
            labels = list(row.get("labels") or [])
            props = dict(row.get("props") or {})
            for key in ("properties_json", "literal_json"):
                if key in props:
                    props[key.removesuffix("_json")] = _decode_json(props.pop(key))
            if "GraphAssertion" in labels:
                if props.get("authority") not in self.policy.authorities:
                    continue
                if not self.policy.include_claims and props.get("assertion_class") == "claim":
                    continue
                if allowed_predicates and props.get("predicate_term_id") not in allowed_predicates:
                    continue
                assertions.append(props)
            elif "GraphEvidence" in labels:
                evidence.append(props)
            else:
                nodes.append({"labels": labels, **props})
        return TaskGraphView(
            view_id=new_graph_id("graph_view"),
            anchor_refs=refs,
            nodes=nodes[: self.policy.max_objects],
            assertions=assertions[: self.policy.max_assertions],
            evidence=evidence[: self.policy.max_evidence],
            as_of_time=as_of_time,
            query_policy=self._policy_dict(max_depth=depth, required_predicates=required_predicates),
        )

    def _policy_dict(self, *, max_depth: int | None, required_predicates: list[str] | None) -> dict[str, Any]:
        return {
            "max_depth": int(max_depth or self.policy.max_depth),
            "max_objects": self.policy.max_objects,
            "max_assertions": self.policy.max_assertions,
            "max_evidence": self.policy.max_evidence,
            "authorities": list(self.policy.authorities),
            "required_predicates": list(required_predicates or []),
        }
