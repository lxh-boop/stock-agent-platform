from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.domain_tools._common import normalize_stock_code


def query_stock_news(
    stock_code: str,
    as_of_date: str | None = None,
    db_path: str | Path | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    # Compatibility wrapper. Agent default path is evidence.search_news via ToolExecutor.
    # planned_removal_phase=post_phase11_1_legacy_cleanup
    from agent_graph.services.evidence_service import evidence_service

    result = evidence_service.search_news(
        normalize_stock_code(stock_code),
        as_of_date=as_of_date,
        db_path=db_path,
        limit=limit,
    )
    data = dict(result.get("data") or {})
    return {
        **result,
        "status": result.get("status") or ("success" if data.get("events") else "no_news"),
        "stock_code": data.get("stock_code") or result.get("stock_code") or normalize_stock_code(stock_code),
        "events": data.get("events") or [],
        "mappings": data.get("mappings") or [],
        "event_count": data.get("event_count") or 0,
        "error": (data.get("error") or ";".join(result.get("errors") or [])),
    }
