from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.evidence_service import evidence_service


def _context_value(args: dict[str, Any], context: dict[str, Any], key: str, default: Any = None) -> Any:
    value = args.get(key)
    if value not in (None, ""):
        return value
    value = context.get(key)
    return default if value in (None, "") else value


def _output_dir(context: dict[str, Any]) -> str | Path:
    return context.get("output_dir") or "outputs"


def _db_path(context: dict[str, Any]) -> str | Path | None:
    return context.get("db_path")


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def evidence_search_news_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return evidence_service.search_news(
        str(args.get("stock_code") or ""),
        as_of_date=args.get("as_of_date"),
        db_path=_db_path(context),
        limit=_int_value(args.get("limit") or args.get("top_k") or context.get("default_top_k"), 10),
    )


def evidence_search_rag_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return evidence_service.search_rag(
        str(args.get("stock_code") or ""),
        query=str(args.get("query") or f"{args.get('stock_code', '')} risk evidence"),
        top_k=_int_value(args.get("top_k") or context.get("default_top_k"), 5),
        output_dir=_output_dir(context),
    )


def evidence_get_stock_evidence_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return evidence_service.get_stock_evidence(
        str(args.get("stock_code") or ""),
        query=str(args.get("query") or ""),
        as_of_date=args.get("as_of_date"),
        top_k=_int_value(args.get("top_k") or context.get("default_top_k"), 5),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )


def evidence_get_market_evidence_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return evidence_service.get_market_evidence(
        query=str(args.get("query") or ""),
        stock_codes=args.get("stock_codes") or args.get("stock_code") or [],
        as_of_date=args.get("as_of_date"),
        top_k=_int_value(args.get("top_k") or context.get("default_top_k"), 5),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )


EvidenceSearchNewsAdapter = evidence_search_news_adapter
EvidenceSearchRagAdapter = evidence_search_rag_adapter
EvidenceGetStockEvidenceAdapter = evidence_get_stock_evidence_adapter
EvidenceGetMarketEvidenceAdapter = evidence_get_market_evidence_adapter
