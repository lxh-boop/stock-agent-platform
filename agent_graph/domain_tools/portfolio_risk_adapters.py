from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.portfolio_risk_service import portfolio_risk_service


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


def portfolio_analyze_risk_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    data = portfolio_risk_service.analyze_current_risk(
        str(_context_value(args, context, "user_id", "default")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )
    status = str(data.get("status") or "").lower()
    success = status == "success"
    return {
        "success": success,
        "message": "Portfolio risk queried." if success else "Portfolio risk query failed.",
        "data": data,
        "warnings": [],
        "errors": [] if success else [status or "portfolio_risk_failed"],
        "tool_name": "portfolio.analyze_risk",
    }


def portfolio_compare_risk_before_after_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    data = portfolio_risk_service.compare_risk_before_after(
        str(_context_value(args, context, "user_id", "default")),
        before=args.get("before") if isinstance(args.get("before"), dict) else None,
        after=args.get("after") if isinstance(args.get("after"), dict) else None,
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )
    success = str(data.get("status") or "").lower() == "success"
    return {
        "success": success,
        "message": "Portfolio risk comparison queried." if success else "Portfolio risk comparison failed.",
        "data": data,
        "warnings": [],
        "errors": [] if success else [str(data.get("status") or "portfolio_risk_compare_failed")],
        "tool_name": "portfolio.compare_risk_before_after",
    }


PortfolioAnalyzeRiskAdapter = portfolio_analyze_risk_adapter
PortfolioCompareRiskBeforeAfterAdapter = portfolio_compare_risk_before_after_adapter
