from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.portfolio_proposal_service import portfolio_proposal_service
from agent_graph.services.top_k import DEFAULT_TOOL_TOP_K


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


def _session_id(context: dict[str, Any]) -> str:
    return str(context.get("session_id") or context.get("conversation_id") or "")


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def portfolio_recommend_position_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return portfolio_proposal_service.recommend_position(
        user_id=str(_context_value(args, context, "user_id", "default")),
        stock_code=str(args.get("stock_code") or ""),
        requested_weight=_float_or_none(args.get("requested_weight")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        top_k=_int_value(args.get("top_k") or context.get("default_top_k"), DEFAULT_TOOL_TOP_K),
    )


def portfolio_recommend_replacement_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return portfolio_proposal_service.recommend_replacement(
        user_id=str(_context_value(args, context, "user_id", "default")),
        stock_code=str(args.get("stock_code") or ""),
        requested_weight=_float_or_none(args.get("requested_weight")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        limit=_int_value(args.get("limit"), 3),
    )


def portfolio_preview_manual_change_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return portfolio_proposal_service.preview_manual_position_change(
        user_id=str(_context_value(args, context, "user_id", "default")),
        stock_code=args.get("stock_code"),
        requested_weight=_float_or_none(args.get("requested_weight")),
        position_adjustment_ratio=_float_or_none(args.get("position_adjustment_ratio")),
        requested_quantity=_float_or_none(args.get("requested_quantity")),
        cash_weight=_float_or_none(args.get("cash_weight")),
        target_position_count=_int_value(args.get("target_position_count"), 0) if args.get("target_position_count") not in (None, "") else None,
        query=str(args.get("query") or context.get("query") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        top_k=_int_value(args.get("top_k") or context.get("default_top_k"), DEFAULT_TOOL_TOP_K),
        session_id=_session_id(context),
    )


def portfolio_preview_rebalance_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return portfolio_proposal_service.preview_rebalance(
        user_id=str(_context_value(args, context, "user_id", "default")),
        stock_code=str(args.get("stock_code") or ""),
        requested_weight=_float_or_none(args.get("requested_weight")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        top_k=_int_value(args.get("top_k") or context.get("default_top_k"), DEFAULT_TOOL_TOP_K),
        session_id=_session_id(context),
    )


def portfolio_preview_adjust_position_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return portfolio_proposal_service.preview_adjust_position(
        user_id=str(_context_value(args, context, "user_id", "default")),
        stock_code=str(args.get("stock_code") or ""),
        requested_weight=_float_or_none(args.get("requested_weight")),
        position_adjustment_ratio=_float_or_none(args.get("position_adjustment_ratio")),
        requested_quantity=_float_or_none(args.get("requested_quantity")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        top_k=_int_value(args.get("top_k") or context.get("default_top_k"), DEFAULT_TOOL_TOP_K),
        session_id=_session_id(context),
    )


def portfolio_preview_paper_trade_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return portfolio_proposal_service.preview_paper_trade(
        user_id=str(_context_value(args, context, "user_id", "default")),
        stock_code=str(args.get("stock_code") or ""),
        requested_weight=_float_or_none(args.get("requested_weight")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        top_k=_int_value(args.get("top_k") or context.get("default_top_k"), DEFAULT_TOOL_TOP_K),
        session_id=_session_id(context),
    )


def portfolio_commit_paper_trade_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return portfolio_proposal_service.commit_paper_trade(
        user_id=str(_context_value(args, context, "user_id", "default")),
        plan_id=str(args.get("plan_id") or ""),
        confirmation_token=str(args.get("confirmation_token") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )
