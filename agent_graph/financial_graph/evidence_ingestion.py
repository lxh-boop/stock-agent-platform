from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from .contracts import (
    GraphAssertionRecord,
    GraphNodeKind,
    GraphRef,
    GraphAuthority,
    GraphEvidenceRecord,
    GraphObjectRecord,
    GraphPatch,
    GraphTermRecord,
    new_graph_id,
)
from .patch_validator import GraphPatchValidator


def _hash_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_id_part(value: Any) -> str:
    text = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "").strip())
    return "_".join(part for part in text.split("_") if part)[:80] or "unknown"


@dataclass(frozen=True)
class ExtractedMention:
    mention_text: str
    resolved_ref: GraphRef | None = None
    role: str = "mentioned"
    confidence: float = 0.5


@dataclass(frozen=True)
class ExtractedAssertion:
    subject_ref: GraphRef
    predicate: str
    object_ref: GraphRef | None = None
    literal_value: Any = None
    polarity: str = "positive"
    modality: str = "asserted"
    confidence: float = 0.5
    properties: dict[str, Any] | None = None


class EvidenceIngestionService:
    """Convert news/announcement/report records into evidence-backed graph patches.

    Entity/event extraction can be implemented by the existing LLM service, a
    deterministic parser, or Neo4j GraphRAG. This class only accepts structured
    extraction results and enforces the write boundary.
    """

    def __init__(self, validator: GraphPatchValidator) -> None:
        self.validator = validator

    def build_patch(
        self,
        *,
        record: dict[str, Any],
        evidence_class: str,
        source_task_id: str,
        source_agent_id: str,
        mentions: list[ExtractedMention] | None = None,
        assertions: list[ExtractedAssertion] | None = None,
        source_ref: str = "",
    ) -> GraphPatch:
        content = str(record.get("content") or record.get("text") or record.get("body") or "")
        title = str(record.get("title") or record.get("headline") or "")
        source = str(record.get("source") or record.get("provider") or evidence_class)
        published_at = str(record.get("published_at") or record.get("publish_time") or record.get("trade_date") or "")
        external_id = str(record.get("news_id") or record.get("document_id") or record.get("source_id") or "")
        content_hash = str(record.get("content_hash") or _hash_text(f"{title}\n{content}"))
        evidence_id = str(record.get("evidence_id") or f"evidence:{_safe_id_part(evidence_class)}:{external_id or content_hash[:24]}")
        content_ref = str(record.get("content_ref") or record.get("url") or f"inline:{content_hash}")
        reliability = record.get("reliability", record.get("source_reliability", 0.5))
        try:
            reliability = max(0.0, min(1.0, float(reliability)))
        except (TypeError, ValueError):
            reliability = 0.5

        evidence = GraphEvidenceRecord(
            evidence_id=evidence_id,
            evidence_class=evidence_class,
            title=title,
            source=source,
            content_ref=content_ref,
            content_hash=content_hash,
            published_at=published_at,
            reliability=reliability,
            properties={
                key: value
                for key, value in record.items()
                if key not in {"content", "text", "body"} and value not in (None, "")
            },
        )

        graph_objects: list[GraphObjectRecord] = []
        graph_terms: list[GraphTermRecord] = []
        graph_assertions: list[GraphAssertionRecord] = []
        source_refs = [item for item in [source_ref, evidence_id] if item]

        # Unresolved mentions are preserved as candidate mention objects only in
        # the discovery/candidate state. Resolved mentions point to canonical refs.
        for index, mention in enumerate(mentions or [], start=1):
            target_id: str
            if mention.resolved_ref is not None:
                target_id = mention.resolved_ref.node_id
            else:
                target_id = f"candidate:mention:{content_hash[:16]}:{index}"
                graph_objects.append(
                    GraphObjectRecord(
                        object_id=target_id,
                        name=mention.mention_text,
                        object_class="unresolved_mention",
                        aliases=[mention.mention_text],
                        properties={"role": mention.role, "evidence_id": evidence_id},
                        authority=GraphAuthority.CANDIDATE,
                        confidence=mention.confidence,
                        source_refs=source_refs,
                    )
                )
            predicate_id = "predicate:MENTIONS"
            if not any(term.term_id == predicate_id for term in graph_terms):
                graph_terms.append(
                    GraphTermRecord(
                        term_id=predicate_id,
                        name="MENTIONS",
                        term_kind="predicate",
                        description="Evidence mentions an object.",
                        source_refs=["system_kernel"],
                    )
                )
            # Evidence is not a GraphObject. Mention links are represented as a
            # claim whose subject is a stable evidence-proxy object.
            proxy_id = f"evidence_proxy:{evidence_id}"
            if not any(obj.object_id == proxy_id for obj in graph_objects):
                graph_objects.append(
                    GraphObjectRecord(
                        object_id=proxy_id,
                        name=title or evidence_id,
                        object_class="evidence_proxy",
                        properties={"evidence_id": evidence_id},
                        authority=GraphAuthority.VALIDATED,
                        confidence=reliability,
                        source_refs=source_refs,
                    )
                )
            graph_assertions.append(
                GraphAssertionRecord(
                    assertion_id=f"assertion:mention:{content_hash[:16]}:{index}",
                    subject_id=proxy_id,
                    predicate_term_id=predicate_id,
                    object_id=target_id,
                    assertion_class="claim",
                    authority=GraphAuthority.CANDIDATE,
                    confidence=mention.confidence,
                    properties={"mention_text": mention.mention_text, "role": mention.role},
                    evidence_ids=[evidence_id],
                    source_refs=source_refs,
                    observed_at=published_at,
                )
            )

        for index, item in enumerate(assertions or [], start=1):
            predicate_name = str(item.predicate or "RELATED_TO").strip().upper()
            predicate_id = f"predicate:{_safe_id_part(predicate_name).upper()}"
            if not any(term.term_id == predicate_id for term in graph_terms):
                graph_terms.append(
                    GraphTermRecord(
                        term_id=predicate_id,
                        name=predicate_name,
                        term_kind="predicate",
                        source_refs=["evidence_extraction"],
                    )
                )
            graph_assertions.append(
                GraphAssertionRecord(
                    assertion_id=f"assertion:{content_hash[:16]}:{index}",
                    subject_id=item.subject_ref.node_id,
                    predicate_term_id=predicate_id,
                    object_id=item.object_ref.node_id if item.object_ref else "",
                    literal_value=item.literal_value,
                    assertion_class="claim",
                    authority=GraphAuthority.CANDIDATE,
                    polarity=item.polarity,
                    modality=item.modality,
                    confidence=item.confidence,
                    properties=dict(item.properties or {}),
                    evidence_ids=[evidence_id],
                    source_refs=source_refs,
                    observed_at=published_at,
                )
            )

        return GraphPatch(
            patch_id=new_graph_id("evidence_patch"),
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
            source_refs=source_refs,
            objects=graph_objects,
            terms=graph_terms,
            assertions=graph_assertions,
            evidence=[evidence],
        )

    def ingest(self, **kwargs: Any) -> dict[str, Any]:
        patch = self.build_patch(**kwargs)
        applied = self.validator.validate_and_apply(patch)
        evidence_refs = [
            GraphRef(
                graph_id=self.validator.store.graph_id,
                node_id=item.evidence_id,
                node_kind=GraphNodeKind.EVIDENCE,
                role="evidence",
                as_of_time=item.published_at,
                source="evidence_ingestion",
                confidence=item.reliability,
                locked=True,
            ).to_dict()
            for item in patch.evidence
        ]
        return {
            **applied,
            "patch_id": patch.patch_id,
            "evidence_refs": evidence_refs,
        }

    def ingest_with_extractor(
        self,
        *,
        record: dict[str, Any],
        extractor: Callable[[dict[str, Any]], dict[str, Any]],
        evidence_class: str,
        source_task_id: str,
        source_agent_id: str,
        source_ref: str = "",
    ) -> dict[str, Any]:
        extracted = dict(extractor(record) or {})
        mentions: list[ExtractedMention] = []
        for raw in extracted.get("mentions") or []:
            if not isinstance(raw, dict):
                continue
            ref = raw.get("resolved_ref")
            mentions.append(
                ExtractedMention(
                    mention_text=str(raw.get("mention_text") or raw.get("text") or ""),
                    resolved_ref=GraphRef.from_dict(ref) if isinstance(ref, dict) else None,
                    role=str(raw.get("role") or "mentioned"),
                    confidence=float(raw.get("confidence") or 0.5),
                )
            )
        assertions: list[ExtractedAssertion] = []
        for raw in extracted.get("assertions") or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("subject_ref"), dict):
                continue
            object_ref = raw.get("object_ref")
            assertions.append(
                ExtractedAssertion(
                    subject_ref=GraphRef.from_dict(raw["subject_ref"]),
                    predicate=str(raw.get("predicate") or "RELATED_TO"),
                    object_ref=GraphRef.from_dict(object_ref) if isinstance(object_ref, dict) else None,
                    literal_value=raw.get("literal_value"),
                    polarity=str(raw.get("polarity") or "positive"),
                    modality=str(raw.get("modality") or "asserted"),
                    confidence=float(raw.get("confidence") or 0.5),
                    properties=dict(raw.get("properties") or {}),
                )
            )
        return self.ingest(
            record=record,
            evidence_class=evidence_class,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
            mentions=mentions,
            assertions=assertions,
            source_ref=source_ref,
        )
