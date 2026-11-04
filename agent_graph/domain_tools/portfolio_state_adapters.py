from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.portfolio_service import portfolio_service


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


def portfolio_get_state_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    data = portfolio_service.get_portfolio_state(
        str(_context_value(args, context, "user_id", "default")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )
    return {
        "success": True,
        "message": "Portfolio state queried.",
        "data": data,
        "warnings": [],
        "errors": [],
        "tool_name": "portfolio.get_state",
    }


def portfolio_get_account_summary_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    data = portfolio_service.get_account_summary(
        str(_context_value(args, context, "user_id", "default")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )
    return {
        "success": bool(data.get("account")),
        "message": "Account summary queried." if data.get("account") else "Account summary is empty.",
        "data": data,
        "warnings": [] if data.get("account") else ["missing_account"],
        "errors": [],
        "tool_name": "portfolio.get_account_summary",
    }


def portfolio_get_positions_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    data = portfolio_service.get_current_positions(
        str(_context_value(args, context, "user_id", "default")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )
    return {
        "success": True,
        "message": "Positions queried.",
        "data": data,
        "warnings": [],
        "errors": [],
        "tool_name": "portfolio.get_positions",
    }


def portfolio_get_orders_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    data = portfolio_service.get_current_orders(
        str(_context_value(args, context, "user_id", "default")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )
    return {
        "success": True,
        "message": "Orders queried.",
        "data": data,
        "warnings": [],
        "errors": [],
        "tool_name": "portfolio.get_orders",
    }


PortfolioGetStateAdapter = portfolio_get_state_adapter
PortfolioGetAccountSummaryAdapter = portfolio_get_account_summary_adapter
PortfolioGetPositionsAdapter = portfolio_get_positions_adapter
PortfolioGetOrdersAdapter = portfolio_get_orders_adapter
