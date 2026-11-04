from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contracts import GraphRef, TaskGraphView
from .view_builder import TaskGraphViewBuilder


@dataclass
class GraphContextProvider:
    view_builder: TaskGraphViewBuilder

    def build_for_task(
        self,
        *,
        focus_refs: list[GraphRef],
        context_refs: list[GraphRef],
        as_of_time: str = "",
        required_predicates: list[str] | None = None,
        max_depth: int | None = None,
    ) -> TaskGraphView:
        anchors: list[GraphRef] = []
        for ref in [*focus_refs, *context_refs]:
            if not any(item.node_id == ref.node_id and item.node_kind == ref.node_kind for item in anchors):
                anchors.append(ref)
        return self.view_builder.build(
            anchors,
            as_of_time=as_of_time,
            required_predicates=required_predicates,
            max_depth=max_depth,
        )

    @staticmethod
    def compact(view: TaskGraphView, *, max_chars: int = 16000) -> dict[str, Any]:
        payload = view.to_dict()
        # Keep structural facts; raw evidence body remains behind content_ref.
        for evidence in payload.get("evidence") or []:
            if isinstance(evidence, dict):
                evidence.pop("content", None)
                evidence.pop("text", None)
                evidence.pop("body", None)
        text = str(payload)
        if len(text) <= max_chars:
            return payload
        return {
            "view_id": view.view_id,
            "anchor_refs": [ref.to_dict() for ref in view.anchor_refs],
            "nodes": view.nodes[:30],
            "assertions": view.assertions[:60],
            "evidence": view.evidence[:20],
            "query_policy": view.query_policy,
            "truncated": True,
        }
