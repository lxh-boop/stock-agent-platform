from __future__ import annotations

from pathlib import Path
from typing import Any

from .memory_context_bridge import (
    build_memory_store_health_summary,
    get_memory_manager_for_output,
)
from .memory_types import MemoryType


def memory_search_adapter(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    output_dir = context.get("output_dir") or args.get("output_dir") or "outputs"
    user_id = str(args.get("user_id") or context.get("user_id") or "default")
    query = str(args.get("query") or "")
    raw_memory_types = args.get("memory_types") or args.get("memory_type") or []
    if isinstance(raw_memory_types, str):
        raw_memory_types = [raw_memory_types]
    memory_types = [MemoryType.from_value(item) for item in raw_memory_types] if isinstance(raw_memory_types, list) else []
    manager = get_memory_manager_for_output(output_dir)
    results = manager.retrieve_for_context(
        user_id=user_id,
        query=query,
        memory_types=memory_types or None,
        topics=list(args.get("topics") or []),
        stock_codes=list(args.get("stock_codes") or []),
        task_type=str(args.get("task_type") or "memory_search"),
        candidate_top_n=int(args.get("candidate_top_n") or args.get("top_n") or 40),
        relevance_threshold=float(args.get("relevance_threshold") or 0.42),
        token_budget=int(args.get("token_budget") or 600),
    )
    return {
        "success": True,
        "tool_name": "memory.search",
        "message": "Memory search completed.",
        "data": {
            "items": results.get("items") or [],
            "item_count": len(results.get("items") or []),
            "policy": results.get("policy") or {},
            "diagnostics": results.get("diagnostics") or {},
            "retrieval_id": results.get("retrieval_id") or "",
            "not_committed": True,
        },
        "warnings": [],
        "errors": [],
        "sources": [],
        "metadata": {
            "read_only": True,
            "store": str(manager.store.describe().get("backend_name") or "memory_store"),
        },
    }


def memory_get_summary_adapter(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    output_dir = Path(context.get("output_dir") or args.get("output_dir") or "outputs")
    user_id = str(args.get("user_id") or context.get("user_id") or "default")
    summary = build_memory_store_health_summary(user_id=user_id, output_dir=output_dir)
    return {
        "success": True,
        "tool_name": "memory.get_summary",
        "message": "Memory summary loaded.",
        "data": {**summary, "not_committed": True},
        "warnings": [],
        "errors": [],
        "sources": [],
        "metadata": {
            "read_only": True,
            "store": str(summary.get("store") or "memory_store"),
        },
    }
