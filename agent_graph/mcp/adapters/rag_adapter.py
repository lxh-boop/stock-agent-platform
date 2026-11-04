from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.evidence_service import evidence_service


class RagMCPAdapter:
    """Expose the existing BM25 + dense + reranker service through MCP."""

    def __init__(
        self,
        *,
        db_path: str | Path | None,
        output_dir: str | Path,
    ) -> None:
        self.db_path = db_path
        self.output_dir = Path(output_dir)

    def search_documents(
        self,
        query: str,
        stock_code: str = "",
        top_k: int = 5,
    ) -> dict[str, Any]:
        return evidence_service.search_documents(
            query,
            stock_code=stock_code,
            top_k=top_k,
            output_dir=self.output_dir,
        )

    def search_news(
        self,
        stock_code: str,
        as_of_date: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        return evidence_service.search_news(
            stock_code,
            as_of_date=as_of_date or None,
            db_path=self.db_path,
            limit=limit,
        )

    def retrieve_evidence(
        self,
        stock_code: str,
        query: str = "",
        as_of_date: str = "",
        top_k: int = 5,
    ) -> dict[str, Any]:
        return evidence_service.get_stock_evidence(
            stock_code,
            query=query,
            as_of_date=as_of_date or None,
            top_k=top_k,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
