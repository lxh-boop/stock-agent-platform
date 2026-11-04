from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.market_analysis_service import market_analysis_service
from agent_graph.services.top_k import DEFAULT_TOOL_TOP_K, resolve_requested_top_k


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
    return resolve_requested_top_k(
        task_top_k=value,
        tool_default_top_k=default,
    )


def _strip_dataframe(result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    data = dict(payload.get("data") or {})
    data.pop("dataframe", None)
    payload["data"] = data
    return payload


def market_get_ranking_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return market_analysis_service.get_ranking(
        stock_code=args.get("stock_code"),
        top_k=resolve_requested_top_k(
            user_explicit_top_k=context.get("user_explicit_top_k"),
            task_top_k=args.get("top_k"),
            request_default_top_k=context.get("default_top_k"),
            tool_default_top_k=DEFAULT_TOOL_TOP_K,
        ),
        output_dir=_output_dir(context),
        model_name=args.get("model_name"),
        db_path=_db_path(context),
    )


def market_analyze_stock_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return market_analysis_service.analyze_stock(
        user_id=str(_context_value(args, context, "user_id", "default")),
        stock_code=str(args.get("stock_code") or ""),
        as_of_date=args.get("as_of_date"),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        top_k=resolve_requested_top_k(
            user_explicit_top_k=context.get("user_explicit_top_k"),
            task_top_k=args.get("top_k"),
            request_default_top_k=context.get("default_top_k"),
            tool_default_top_k=DEFAULT_TOOL_TOP_K,
        ),
        include_rag=bool(args.get("include_rag", True)),
        tool_name="market.analyze_stock",
    )


def market_lookup_stock_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return market_analysis_service.lookup_stock(
        str(args.get("stock_query") or args.get("stock_code") or ""),
        user_id=str(_context_value(args, context, "user_id", "default")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )


def market_compare_stocks_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return market_analysis_service.compare_stocks(
        args.get("stock_codes") or args.get("stock_code") or [],
        user_id=str(_context_value(args, context, "user_id", "default")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        top_k=resolve_requested_top_k(
            user_explicit_top_k=context.get("user_explicit_top_k"),
            task_top_k=args.get("top_k"),
            request_default_top_k=context.get("default_top_k"),
            tool_default_top_k=DEFAULT_TOOL_TOP_K,
        ),
    )


def market_signal_summary_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return _strip_dataframe(
        market_analysis_service.get_signal_summary(
            output_dir=_output_dir(context),
            user_id=str(_context_value(args, context, "user_id", "default")),
            sort_by=str(args.get("sort_by") or "original_rank"),
            include_dataframe=False,
            db_path=_db_path(context),
        )
    )


MarketGetRankingAdapter = market_get_ranking_adapter
MarketAnalyzeStockAdapter = market_analyze_stock_adapter
MarketLookupStockAdapter = market_lookup_stock_adapter
MarketCompareStocksAdapter = market_compare_stocks_adapter
MarketSignalSummaryAdapter = market_signal_summary_adapter
