from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .memory_manager import MemoryManager
from .memory_retrieval_types import MemoryRetrievalRequest
from .memory_sanitizer import MemorySanitizer

_STOCK_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")


def get_memory_manager_for_output(
    output_dir: str | Path = "outputs",
) -> MemoryManager:
    # output_dir remains an application-context argument only. Persistent memory
    # location is owned by the configured MemoryStore backend.
    del output_dir
    return MemoryManager()


def build_memory_context_view(
    *,
    user_id: str,
    query: str = "",
    output_dir: str | Path = "outputs",
    conversation_id: str = "",
    run_id: str = "",
    task_type: str = "",
    agent_role: str = "supervisor",
    entities: dict[str, Any] | None = None,
    topics: list[str] | None = None,
    stock_codes: list[str] | None = None,
    candidate_top_n: int = 40,
    relevance_threshold: float = 0.42,
    token_budget: int = 360,
    request: MemoryRetrievalRequest | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build the only LLM-visible memory view for one request.

    ``limit`` remains as an input compatibility alias for candidate_top_n.  It
    never means a fixed number of memories admitted to context.
    """

    manager = get_memory_manager_for_output(output_dir)
    codes = list(stock_codes or [])
    codes.extend(_STOCK_RE.findall(str(query or "")))
    req = request or MemoryRetrievalRequest(
        user_id=str(user_id or "default"),
        query=str(query or ""),
        conversation_id=str(conversation_id or ""),
        run_id=str(run_id or ""),
        task_type=str(task_type or ""),
        agent_role=str(agent_role or "supervisor"),
        entities=dict(entities or {}),
        topics=list(topics or []),
        stock_codes=list(dict.fromkeys(codes)),
        candidate_top_n=int(limit or candidate_top_n),
        relevance_threshold=relevance_threshold,
        token_budget=token_budget,
    )
    view = manager.retrieve_for_context(request=req)
    items = list(view.get("items") or [])
    memory_refs = [
        str((item.get("memory") or {}).get("memory_id") or "")
        for item in items
        if (item.get("memory") or {}).get("memory_id")
    ]
    diagnostics = dict(view.get("diagnostics") or {})
    return {
        "retrieval_id": str(view.get("retrieval_id") or ""),
        "memory_refs": memory_refs,
        "item_count": len(items),
        "items": items,
        "diagnostics": diagnostics,
        "policy": view.get("policy") or {},
    }


def build_memory_store_health_summary(
    *,
    user_id: str = "default",
    output_dir: str | Path = "outputs",
) -> dict[str, Any]:
    try:
        manager = get_memory_manager_for_output(output_dir)
        store = manager.store
        descriptor = dict(store.describe() or {})
        total_count = store.count()
        user_count = store.count(user_id=user_id)
        candidate_count = store.count(user_id=user_id, status="CANDIDATE")
        latest = store.list_records(user_id=user_id, limit=5)
        return {
            "status": "ok",
            "store": str(descriptor.get("backend_name") or getattr(store, "backend_name", "memory_store")),
            "storage_kind": str(descriptor.get("storage_kind") or "unknown"),
            "exists": bool(descriptor.get("available", True)),
            "total_count": total_count,
            "user_count": user_count,
            "candidate_count": candidate_count,
            "latest_memory_count": len(latest),
            "latest_memory_types": sorted(
                {record.memory_type.value for record in latest}
            ),
            "secret_safe": True,
            "write_permission": "none",
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "store": "memory_store",
            "storage_kind": "unknown",
            "exists": False,
            "total_count": 0,
            "user_count": 0,
            "candidate_count": 0,
            "latest_memory_count": 0,
            "latest_memory_types": [],
            "secret_safe": True,
            "write_permission": "none",
            "error": type(exc).__name__,
        }


def build_memory_safe_summary(
    *,
    user_id: str = "default",
    output_dir: str | Path = "outputs",
) -> str:
    health = build_memory_store_health_summary(
        user_id=user_id,
        output_dir=output_dir,
    )
    return (
        "Memory safe summary: "
        f"status={health.get('status')} | "
        f"user_records={health.get('user_count')} | "
        f"latest={health.get('latest_memory_count')} | "
        f"write_permission={health.get('write_permission')} | "
        f"secret_safe={health.get('secret_safe')}"
    )


def list_memory_records_safe_page(
    *,
    user_id: str = "default",
    output_dir: str | Path = "outputs",
    limit: int = 5,
    offset: int = 0,
) -> dict[str, Any]:
    try:
        store = get_memory_manager_for_output(output_dir).store
        total = store.count(user_id=user_id)
        start = max(0, int(offset or 0))
        page_size = max(1, min(50, int(limit or 5)))
        records = store.list_records(
            user_id=user_id,
            limit=start + page_size,
        )
        sanitizer = MemorySanitizer(max_text_chars=300)
        rows = []
        for record in records[start : start + page_size]:
            safe = sanitizer.sanitize_for_ui(record)
            rows.append(
                {
                    "memory_id": str(safe.get("memory_id") or "")[:96],
                    "memory_type": str(safe.get("memory_type") or "")[:64],
                    "memory_subtype": str(
                        safe.get("memory_subtype") or ""
                    )[:64],
                    "scope": str(safe.get("scope") or "")[:64],
                    "summary": str(
                        safe.get("summary") or safe.get("content") or ""
                    )[:220],
                    "topics": list(safe.get("topics") or [])[:8],
                    "stock_codes": list(safe.get("stock_codes") or [])[:8],
                    "importance": safe.get("importance"),
                    "confidence": safe.get("confidence"),
                    "updated_at": str(safe.get("updated_at") or "")[:19],
                }
            )
        return {
            "status": "ok",
            "total_count": total,
            "offset": start,
            "limit": page_size,
            "records": rows,
            "safety": {
                "secrets_redacted": True,
                "raw_paths_hidden": True,
                "raw_payload_hidden": True,
            },
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "total_count": 0,
            "offset": max(0, int(offset or 0)),
            "limit": max(1, min(50, int(limit or 5))),
            "records": [],
            "error": type(exc).__name__,
            "safety": {
                "secrets_redacted": True,
                "raw_paths_hidden": True,
                "raw_payload_hidden": True,
            },
        }


def extract_memory_candidates_from_message_trace(
    messages: list[dict[str, Any]],
    *,
    user_id: str,
    output_dir: str | Path = "outputs",
) -> list[dict[str, Any]]:
    manager = get_memory_manager_for_output(output_dir)
    candidates = []
    for message in messages:
        candidates.extend(
            manager.remember_candidate(
                message,
                user_id=user_id,
                source_type="message",
            )
        )
    return [candidate.to_dict() for candidate in candidates]


def extract_memory_candidates_from_artifact(
    artifact: dict[str, Any],
    *,
    user_id: str,
    output_dir: str | Path = "outputs",
) -> list[dict[str, Any]]:
    manager = get_memory_manager_for_output(output_dir)
    candidates = manager.remember_candidate(
        {"user_id": user_id, **dict(artifact or {})},
        user_id=user_id,
        source_type="artifact",
    )
    return [candidate.to_dict() for candidate in candidates]


__all__ = [
    "build_memory_context_view",
    "build_memory_safe_summary",
    "build_memory_store_health_summary",
    "extract_memory_candidates_from_artifact",
    "extract_memory_candidates_from_message_trace",
    "get_memory_manager_for_output",
    "list_memory_records_safe_page",
]
