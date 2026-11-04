"""Atomic read-only evidence tools privately available to W01.

Collection sources are separate Tool capabilities so W01 can dynamically plan a
single-node or multi-source Tool DAG. Finalization is a pure normalization and
coverage-validation step; no Tool writes Neo4j or business state.
"""

from __future__ import annotations

from typing import Any, Callable

import hashlib

from agent_graph.workers.cards import EVIDENCE_COLLECTOR
from agent_graph.financial_graph.contracts import GraphNodeKind, GraphRef, refs_from
from agent_graph.financial_graph.provider_adapter import GraphProviderAdapter
from agent_graph.mcp.worker_adapter import invoke_worker_mcp
from agent_graph.toolbox import (
    OP_READ,
    TOOL_VISIBILITY_WORKER_PRIVATE,
    ToolDefinition,
    ToolInputContract,
    ToolOutputContract,
    description,
    result_schema,
    schema,
)


EVIDENCE_SEARCH_NEWS_TOOL = "evidence.search_news"
EVIDENCE_SEARCH_RAG_TOOL = "evidence.search_rag"
EVIDENCE_FINALIZE_COLLECTION_TOOL = "evidence.finalize_collection"


def _object_refs(arguments: dict[str, Any]) -> list[GraphRef]:
    refs = [
        ref
        for ref in refs_from(arguments.get("object_refs") or [])
        if ref.node_kind == GraphNodeKind.OBJECT
    ]
    if not refs:
        raise ValueError("object_refs_required")
    return refs


def _raw_data(raw: dict[str, Any]) -> dict[str, Any]:
    return dict(raw.get("data") or {}) if isinstance(raw.get("data"), dict) else dict(raw)


def _source_result(
    *,
    provider: GraphProviderAdapter,
    refs: list[GraphRef],
    source_name: str,
    collector: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Normalize one evidence source without treating a valid empty query as failure."""

    results: list[dict[str, Any]] = []
    hard_failure_count = 0
    for ref in refs:
        code = provider.provider_symbol(ref)
        raw = collector(code)
        data = _raw_data(raw)
        records = list(data.get("records") or raw.get("records") or [])
        sources = list(data.get("sources") or raw.get("sources") or [])
        errors = [str(item) for item in raw.get("errors") or []]
        status = str(raw.get("status") or "").strip().lower()
        hard_failure = bool(errors) and status in {
            "error",
            "unavailable",
            "invalid_stock_code",
            "failed",
        }
        execution_success = not hard_failure
        if hard_failure:
            hard_failure_count += 1
        results.append(
            {
                "focus_ref": ref.to_dict(),
                "source_name": source_name,
                "success": execution_success,
                "business_empty": bool(execution_success and not records),
                "status": status,
                "message": str(raw.get("message") or ""),
                "records": records,
                "sources": sources,
                "warnings": [str(item) for item in raw.get("warnings") or []],
                "errors": errors,
                "retrieval_diagnostics": dict(data.get("retrieval_diagnostics") or {}),
            }
        )
    execution_success_count = sum(1 for item in results if item["success"])
    return {
        "success": execution_success_count > 0 and hard_failure_count < len(results),
        "message": (
            f"{source_name} queried."
            if hard_failure_count == 0
            else f"{source_name} partially queried."
            if execution_success_count
            else f"{source_name} query failed."
        ),
        "data": {
            "results": results,
            "source_name": source_name,
            "source_collection_count": len(results),
            "source_success_count": execution_success_count,
            "source_failure_count": hard_failure_count,
            "business_empty": bool(results) and all(item["business_empty"] for item in results),
            "write_performed": False,
            "retrieval_diagnostics": [
                dict(item.get("retrieval_diagnostics") or {})
                for item in results
                if item.get("retrieval_diagnostics")
            ],
        },
        "warnings": [
            warning
            for item in results
            for warning in item.get("warnings") or []
        ],
        "errors": [
            error
            for item in results
            for error in item.get("errors") or []
        ] if execution_success_count == 0 else [],
        "error_type": "evidence_source_query_failed" if execution_success_count == 0 else "",
        "error_message": "All entity queries for this evidence source failed." if execution_success_count == 0 else "",
        "failure_kind": "tool_failure" if execution_success_count == 0 else "",
        "retryable": bool(execution_success_count == 0),
    }


def _envelope_data(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    data = value.get("data")
    if isinstance(data, dict):
        return data
    return value


_IDENTITY_FIELDS = ("news_id", "source_id", "graph_evidence_key")


def _identity_values(row: dict[str, Any]) -> list[str]:
    """Return stable evidence ids shared by direct-news and RAG records.

    A RAG chunk may expose the same underlying news id through ``news_id`` while
    the direct-news record exposes it through ``source_id``.  Deduplication is
    therefore based on the values, not on the field names.
    """

    values: list[str] = []
    for key in _IDENTITY_FIELDS:
        value = str(row.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return values


def _fallback_record_id(row: dict[str, Any], *, sequence: int) -> str:
    """Create an audit-only id for records that have no stable source id.

    The fallback deliberately includes ``sequence`` so id-less records are not
    silently merged by text similarity.  The user-requested deduplication rule
    is identity based, not semantic similarity based.
    """

    raw = "|".join(
        str(row.get(key) or "").strip()
        for key in ("url", "title", "section_title", "publish_time", "trade_date")
    )
    digest = hashlib.sha256(f"{sequence}|{raw}".encode("utf-8")).hexdigest()[:20]
    return f"unidentified:{digest}"


def _source_key(row: dict[str, Any]) -> tuple[str, ...]:
    ids = _identity_values(row)
    if ids:
        return ("identity", *sorted(ids))
    return tuple(
        str(row.get(key) or "").strip()
        for key in ("source_type", "url", "title", "source")
    )


def _content_length(row: dict[str, Any]) -> int:
    return max(
        [
            len(str(row.get(key) or ""))
            for key in ("text", "content", "chunk_text", "summary")
        ]
        or [0]
    )


def _merge_record(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge duplicate records without losing provenance or the richest text."""

    merged = dict(base)
    if _content_length(incoming) > _content_length(merged):
        for key in ("text", "content", "chunk_text", "summary", "content_level"):
            if incoming.get(key) not in (None, "", [], {}):
                merged[key] = incoming.get(key)
    for key, value in incoming.items():
        if key.startswith("_"):
            continue
        if merged.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            merged[key] = value
    try:
        merged_score = float(merged.get("score", merged.get("mapping_confidence", 0.0)) or 0.0)
    except (TypeError, ValueError):
        merged_score = 0.0
    try:
        incoming_score = float(incoming.get("score", incoming.get("mapping_confidence", 0.0)) or 0.0)
    except (TypeError, ValueError):
        incoming_score = 0.0
    if incoming_score > merged_score:
        if incoming.get("score") not in (None, ""):
            merged["score"] = incoming.get("score")
        elif incoming.get("mapping_confidence") not in (None, ""):
            merged["mapping_confidence"] = incoming.get("mapping_confidence")
    return merged


def _rank_key(row: dict[str, Any]) -> tuple[float, str]:
    try:
        score = float(row.get("score", row.get("mapping_confidence", 0.0)) or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    date = str(row.get("publish_time") or row.get("trade_date") or row.get("date") or "")
    return score, date


def _deduplicate_records(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collapse records that share any stable evidence id.

    This is intentionally identity-only normalization.  It does not infer that
    two different ids describe the same event and therefore does not encode
    business or semantic judgment in program rules.
    """

    normalized = [dict(row) for row in rows if isinstance(row, dict)]
    parent = list(range(len(normalized)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    id_owner: dict[str, int] = {}
    identified_record_count = 0
    for index, row in enumerate(normalized):
        ids = _identity_values(row)
        if ids:
            identified_record_count += 1
        for identity in ids:
            owner = id_owner.get(identity)
            if owner is None:
                id_owner[identity] = index
            else:
                union(owner, index)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, row in enumerate(normalized):
        groups.setdefault(find(index), []).append(row)

    canonical_records: list[dict[str, Any]] = []
    duplicate_groups: list[dict[str, Any]] = []
    source_record_counts: dict[str, int] = {}
    for row in normalized:
        source_name = str(row.get("_retrieved_by") or "unknown")
        source_record_counts[source_name] = source_record_counts.get(source_name, 0) + 1

    for group_rows in groups.values():
        ranked = sorted(
            group_rows,
            key=lambda row: (_rank_key(row), _content_length(row)),
            reverse=True,
        )
        merged = dict(ranked[0])
        all_ids: list[str] = []
        retrieved_by: list[str] = []
        for row in ranked:
            merged = _merge_record(merged, row)
            for identity in _identity_values(row):
                if identity not in all_ids:
                    all_ids.append(identity)
            source_name = str(row.get("_retrieved_by") or "").strip()
            if source_name and source_name not in retrieved_by:
                retrieved_by.append(source_name)
        merged.pop("_retrieved_by", None)
        canonical_id = all_ids[0] if all_ids else _fallback_record_id(merged, sequence=len(canonical_records))
        merged["canonical_id"] = canonical_id
        merged["source_ids"] = all_ids
        merged["retrieved_by"] = retrieved_by
        merged["merged_record_count"] = len(group_rows)
        canonical_records.append(merged)
        if len(group_rows) > 1:
            duplicate_groups.append({
                "canonical_id": canonical_id,
                "source_ids": all_ids,
                "retrieved_by": retrieved_by,
                "merged_record_count": len(group_rows),
            })

    canonical_records.sort(key=_rank_key, reverse=True)
    raw_record_count = len(normalized)
    canonical_record_count = len(canonical_records)
    diagnostics = {
        "policy": "shared_identity_value",
        "identity_fields": list(_IDENTITY_FIELDS),
        "raw_record_count": raw_record_count,
        "identified_record_count": identified_record_count,
        "unidentified_record_count": raw_record_count - identified_record_count,
        "canonical_record_count": canonical_record_count,
        "duplicate_record_count": raw_record_count - canonical_record_count,
        "duplicate_group_count": len(duplicate_groups),
        "cross_source_duplicate_group_count": sum(
            1 for item in duplicate_groups if len(item.get("retrieved_by") or []) > 1
        ),
        "source_record_counts": source_record_counts,
        "duplicate_groups": duplicate_groups[:20],
    }
    return canonical_records, diagnostics


def build_evidence_tool_definitions(
    provider: GraphProviderAdapter,
) -> list[ToolDefinition]:
    """Bind atomic W01 tools to run-scoped dependencies."""

    def search_news(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        refs = _object_refs(arguments)
        top_k = max(1, min(int(arguments.get("top_k") or 20), 100))
        as_of_time = str(arguments.get("as_of_time") or "")
        return _source_result(
            provider=provider,
            refs=refs,
            source_name="news_and_announcements",
            collector=lambda code: invoke_worker_mcp(
                "rag",
                "search_news",
                {
                    "stock_code": code,
                    "as_of_date": as_of_time,
                    "limit": top_k,
                },
                context,
                caller_tool_id=EVIDENCE_SEARCH_NEWS_TOOL,
            ),
        )

    def search_rag(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        refs = _object_refs(arguments)
        top_k = max(1, min(int(arguments.get("top_k") or 20), 100))
        query = str(arguments.get("query") or "")
        return _source_result(
            provider=provider,
            refs=refs,
            source_name="rag_evidence",
            collector=lambda code: invoke_worker_mcp(
                "rag",
                "search_documents",
                {
                    "stock_code": code,
                    "query": query or code,
                    "top_k": top_k,
                },
                context,
                caller_tool_id=EVIDENCE_SEARCH_RAG_TOOL,
            ),
        )

    def finalize_collection(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        required_refs = _object_refs({"object_refs": arguments.get("required_object_refs") or []})
        collections = arguments.get("collections") or []
        if not isinstance(collections, list):
            raise ValueError("evidence_collections_list_required")
        source_collection_count = len(collections)
        normalized_collections: list[dict[str, Any]] = []
        for collection in collections:
            if not isinstance(collection, dict):
                raise ValueError("evidence_collection_semantic_payload_required")
            normalized_collections.append(dict(collection))
        collections = normalized_collections
        # Tool-DAG dependencies only expose materialized semantic output slots.
        # A failed upstream source Tool therefore blocks this finalizer rather than
        # passing a second legacy envelope shape into the transform. Valid empty
        # source reads are ordinary successful semantic payloads.
        source_success_count = source_collection_count
        source_failure_count = 0
        grouped: dict[str, dict[str, Any]] = {
            ref.node_id: {
                "focus_ref": ref.to_dict(),
                "success": False,
                "message": "",
                "records": [],
                "sources": [],
                "warnings": [],
                "errors": [],
                "source_names": [],
            }
            for ref in required_refs
        }
        for envelope in collections:
            data = _envelope_data(envelope)
            for item in data.get("results") or []:
                if not isinstance(item, dict):
                    continue
                focus_ref = item.get("focus_ref") if isinstance(item.get("focus_ref"), dict) else {}
                node_id = str(focus_ref.get("node_id") or "")
                if node_id not in grouped:
                    continue
                target = grouped[node_id]
                target["success"] = bool(target["success"] or item.get("success"))
                target["message"] = str(item.get("message") or target["message"])
                source_name = str(item.get("source_name") or data.get("source_name") or "")
                for row in item.get("records") or []:
                    if isinstance(row, dict):
                        enriched = dict(row)
                        enriched["_retrieved_by"] = source_name or "unknown"
                        target["records"].append(enriched)
                for row in item.get("sources") or []:
                    if isinstance(row, dict):
                        enriched = dict(row)
                        enriched["_retrieved_by"] = source_name or "unknown"
                        target["sources"].append(enriched)
                target["warnings"].extend(str(value) for value in item.get("warnings") or [])
                target["errors"].extend(str(value) for value in item.get("errors") or [])
                if source_name and source_name not in target["source_names"]:
                    target["source_names"].append(source_name)

        normalized_results: list[dict[str, Any]] = []
        all_warnings: list[str] = []
        all_errors: list[str] = []
        aggregate_deduplication = {
            "policy": "shared_identity_value",
            "identity_fields": list(_IDENTITY_FIELDS),
            "raw_record_count": 0,
            "identified_record_count": 0,
            "unidentified_record_count": 0,
            "canonical_record_count": 0,
            "duplicate_record_count": 0,
            "duplicate_group_count": 0,
            "cross_source_duplicate_group_count": 0,
            "source_record_counts": {},
            "duplicate_groups": [],
        }
        for ref in required_refs:
            item = grouped[ref.node_id]
            deduped_records, deduplication = _deduplicate_records(
                [row for row in item["records"] if isinstance(row, dict)]
            )
            seen_sources: set[tuple[str, ...]] = set()
            deduped_sources: list[dict[str, Any]] = []
            for row in [row for row in item["sources"] if isinstance(row, dict)]:
                key = _source_key(row)
                if key in seen_sources:
                    continue
                seen_sources.add(key)
                cleaned = dict(row)
                cleaned.pop("_retrieved_by", None)
                deduped_sources.append(cleaned)
            warnings = list(dict.fromkeys(item["warnings"]))
            errors = list(dict.fromkeys(item["errors"]))
            all_warnings.extend(warnings)
            all_errors.extend(errors)
            for key in (
                "raw_record_count", "identified_record_count", "unidentified_record_count",
                "canonical_record_count", "duplicate_record_count", "duplicate_group_count",
                "cross_source_duplicate_group_count",
            ):
                aggregate_deduplication[key] += int(deduplication.get(key) or 0)
            for source_name, count in dict(deduplication.get("source_record_counts") or {}).items():
                source_counts = aggregate_deduplication["source_record_counts"]
                source_counts[source_name] = int(source_counts.get(source_name) or 0) + int(count or 0)
            aggregate_deduplication["duplicate_groups"].extend(
                list(deduplication.get("duplicate_groups") or [])[:20]
            )
            aggregate_deduplication["duplicate_groups"] = aggregate_deduplication["duplicate_groups"][:20]
            normalized_results.append(
                {
                    "focus_ref": ref.to_dict(),
                    "success": bool(deduped_records),
                    "message": item["message"] or ("Evidence collected." if deduped_records else "No evidence found."),
                    "records": deduped_records,
                    "sources": deduped_sources,
                    "source_names": list(item["source_names"]),
                    "deduplication": deduplication,
                    "warnings": warnings,
                    "errors": errors,
                }
            )
        covered_ids = {
            str(item.get("focus_ref", {}).get("node_id") or "")
            for item in normalized_results
            if item.get("records")
        }
        required_ids = {ref.node_id for ref in required_refs}
        record_count = sum(len(item.get("records") or []) for item in normalized_results)
        source_count = sum(len(item.get("sources") or []) for item in normalized_results)
        coverage = {
            "required_entity_count": len(required_ids),
            "covered_entity_count": len(covered_ids),
            "missing_entity_ref_ids": sorted(required_ids - covered_ids),
            "coverage_satisfied": required_ids.issubset(covered_ids),
        }
        all_sources_failed = bool(source_collection_count) and source_success_count == 0 and source_failure_count >= source_collection_count
        business_empty = record_count == 0 and not all_sources_failed
        return {
            "success": not all_sources_failed,
            "message": (
                "External evidence collection finalized."
                if record_count
                else "All selected evidence sources failed."
                if all_sources_failed
                else "No external evidence was found."
            ),
            "data": {
                "results": normalized_results,
                "record_count": record_count,
                "source_count": source_count,
                "deduplication": aggregate_deduplication,
                "coverage": coverage,
                "source_collection_count": source_collection_count,
                "source_success_count": source_success_count,
                "source_failure_count": source_failure_count,
                "business_empty": business_empty,
                "warnings": list(dict.fromkeys(all_warnings)),
                "errors": list(dict.fromkeys(all_errors)),
                "write_performed": False,
                "validated_evidence_collection": True,
            },
            "warnings": list(dict.fromkeys(all_warnings)),
            "errors": list(dict.fromkeys(all_errors)) if all_sources_failed else [],
            "error_type": "all_evidence_sources_failed" if all_sources_failed else "",
            "error_message": "All selected evidence source tools failed." if all_sources_failed else "",
            "failure_kind": "tool_failure" if all_sources_failed else "",
            "retryable": bool(all_sources_failed),
        }


    common_source_schema = schema(
        {
            "object_refs": {"type": "array", "description": "Authoritative financial-object GraphRefs."},
            "query": {"type": "string"},
            "top_k": {"type": "integer"},
            "as_of_time": {"type": "string"},
        },
        required=["object_refs"],
    )
    return [
        ToolDefinition(
            name=EVIDENCE_SEARCH_NEWS_TOOL,
            display_name="Search News and Announcements",
            description=description(
                "Read mapped news and announcement evidence for an authoritative entity set.",
                "W01 needs recent news, announcements, governance events, or public event records.",
                "RAG-only retrieval, interpretation, graph persistence, portfolio work, or proposals.",
                "object_refs, optional as_of_time, and top_k.",
                "Per-entity news and announcement records with source metadata.",
            ),
            input_schema=common_source_schema,
            output_schema=result_schema(["results"]),
            execution_handler=search_news,
            supported_actions=["search_news", "search_announcements"],
            supported_objects=["financial_object_graph_ref_set"],
            produced_outputs=["results", "news_evidence"],
            input_contracts=[
                ToolInputContract(
                    slot_id="object_refs",
                    required=True,
                    contract="graph.object-refs",
                    version="1.0",
                ),
                ToolInputContract(slot_id="query", required=False),
                ToolInputContract(slot_id="top_k", required=False),
                ToolInputContract(slot_id="as_of_time", required=False),
            ],
            output_contracts=[
                ToolOutputContract(
                    slot_id="results",
                    source_path="data.results",
                    contract="evidence.source-results",
                    version="1.0",
                ),
                ToolOutputContract(
                    slot_id="news_evidence",
                    schema_id="EvidenceSourceCollection.v1",
                    source_path="data",
                    contract="evidence.source-collection",
                    version="1.0",
                ),
            ],
            operation_type=OP_READ,
            allowed_agent_types=[EVIDENCE_COLLECTOR],
            permission_scope=OP_READ,
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            side_effects=[],
            mutates_business_state=False,
            idempotency="read_only",
            audit_level="full",
            runtime_policy={
                "provider_type": "mcp",
                "server_id": "rag",
                "transport_tool_name": "search_news",
                "transport": "stdio",
                "projection_kind": "worker_adapter",
            },
            tags=["worker_private", "evidence", "news", "atomic", "read_only"],
        ),
        ToolDefinition(
            name=EVIDENCE_SEARCH_RAG_TOOL,
            display_name="Search RAG Evidence",
            description=description(
                "Read indexed RAG evidence for an authoritative entity set and research query.",
                "W01 needs semantically relevant indexed evidence, report chunks, or long-form context.",
                "Direct news-only retrieval, interpretation, graph persistence, portfolio work, or proposals.",
                "object_refs, query, top_k, and optional as_of_time.",
                "Per-entity RAG records with retrieval scores and source metadata.",
            ),
            input_schema=schema(
                common_source_schema["properties"],
                required=["object_refs", "query"],
            ),
            output_schema=result_schema(["results"]),
            execution_handler=search_rag,
            supported_actions=["search_rag_evidence"],
            supported_objects=["financial_object_graph_ref_set"],
            produced_outputs=["results", "rag_evidence"],
            input_contracts=[
                ToolInputContract(
                    slot_id="object_refs",
                    required=True,
                    contract="graph.object-refs",
                    version="1.0",
                ),
                ToolInputContract(slot_id="query", required=True),
                ToolInputContract(slot_id="top_k", required=False),
                ToolInputContract(slot_id="as_of_time", required=False),
            ],
            output_contracts=[
                ToolOutputContract(
                    slot_id="results",
                    source_path="data.results",
                    contract="evidence.source-results",
                    version="1.0",
                ),
                ToolOutputContract(
                    slot_id="rag_evidence",
                    schema_id="EvidenceSourceCollection.v1",
                    source_path="data",
                    contract="evidence.source-collection",
                    version="1.0",
                ),
            ],
            operation_type=OP_READ,
            allowed_agent_types=[EVIDENCE_COLLECTOR],
            permission_scope=OP_READ,
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            side_effects=[],
            mutates_business_state=False,
            idempotency="read_only",
            audit_level="full",
            runtime_policy={
                "provider_type": "mcp",
                "server_id": "rag",
                "transport_tool_name": "search_documents",
                "transport": "stdio",
                "projection_kind": "worker_adapter",
            },
            tags=["worker_private", "evidence", "rag", "atomic", "read_only"],
        ),
        ToolDefinition(
            name=EVIDENCE_FINALIZE_COLLECTION_TOOL,
            display_name="Finalize Evidence Collection",
            description=description(
                "Merge, deduplicate, rank, and validate one or more source-collection results.",
                "W01 has completed one or more source reads and must produce its final evidence collection.",
                "Source retrieval, evidence interpretation, graph persistence, portfolio work, or proposals.",
                "collections and required_object_refs.",
                "Validated per-entity evidence collection, counts, warnings, and coverage.",
            ),
            input_schema=schema(
                {
                    "collections": {"type": "array"},
                    "required_object_refs": {"type": "array"},
                    "collection_goal": {"type": "string"},
                },
                required=["collections", "required_object_refs"],
            ),
            output_schema=result_schema(
                [
                    "results",
                    "record_count",
                    "source_count",
                    "deduplication",
                    "coverage",
                    "validated_evidence_collection",
                ]
            ),
            execution_handler=finalize_collection,
            supported_actions=["finalize_evidence_collection"],
            supported_objects=["evidence_source_result_set"],
            produced_outputs=[
                "results",
                "record_count",
                "source_count",
                "deduplication",
                "coverage",
                "validated_evidence_collection",
            ],
            input_contracts=[
                ToolInputContract(
                    slot_id="collections",
                    schema_id="EvidenceSourceCollection.v1",
                    required=True,
                    accepted_sources=("upstream_tool",),
                    cardinality="many",
                    description="One or more evidence-source semantic payloads selected by the Tool DAG.",
                    contract="evidence.source-collection",
                    version="1.0",
                ),
                ToolInputContract(slot_id="required_object_refs", required=True),
                ToolInputContract(slot_id="collection_goal", required=False),
            ],
            output_contracts=[
                ToolOutputContract(slot_id="results", source_path="data.results"),
                ToolOutputContract(slot_id="record_count", source_path="data.record_count"),
                ToolOutputContract(slot_id="source_count", source_path="data.source_count"),
                ToolOutputContract(slot_id="deduplication", source_path="data.deduplication"),
                ToolOutputContract(slot_id="coverage", source_path="data.coverage"),
                ToolOutputContract(slot_id="validated_evidence_collection", source_path="data.validated_evidence_collection"),
            ],
            operation_type=OP_READ,
            allowed_agent_types=[EVIDENCE_COLLECTOR],
            permission_scope=OP_READ,
            visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
            side_effects=[],
            mutates_business_state=False,
            idempotency="pure_transform",
            audit_level="full",
            tags=["worker_private", "evidence", "finalizer", "atomic", "read_only"],
        ),

    ]


__all__ = [
    "EVIDENCE_SEARCH_NEWS_TOOL",
    "EVIDENCE_SEARCH_RAG_TOOL",
    "EVIDENCE_FINALIZE_COLLECTION_TOOL",
    "build_evidence_tool_definitions",
]
