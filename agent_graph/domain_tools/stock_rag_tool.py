from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.domain_tools._common import normalize_stock_code


def query_stock_rag(
    stock_code: str,
    query: str = "",
    top_k: int = 5,
    output_dir: str | Path = "outputs",
) -> dict[str, Any]:
    # Compatibility wrapper. Agent default path is evidence.search_rag via ToolExecutor.
    # planned_removal_phase=post_phase11_1_legacy_cleanup
    from agent_graph.services.evidence_service import evidence_service

    result = evidence_service.search_rag(
        normalize_stock_code(stock_code),
        query=query,
        top_k=int(top_k),
        output_dir=output_dir,
    )
    data = dict(result.get("data") or {})
    return {
        **result,
        "status": result.get("status") or ("success" if data.get("chunks") else "no_rag_chunks"),
        "stock_code": data.get("stock_code") or result.get("stock_code") or normalize_stock_code(stock_code),
        "chunks": data.get("chunks") or [],
        "error": data.get("error") or ";".join(result.get("errors") or []),
    }
