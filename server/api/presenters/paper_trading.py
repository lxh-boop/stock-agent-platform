from __future__ import annotations

from typing import Any

from server.api.presenters.common import table_payload, to_browser_value


def present_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    data = dict(value or {})
    return {
        "user_id": str(data.get("user_id") or ""),
        "is_available": bool(data.get("is_available")),
        "account": to_browser_value(data.get("account") or {}),
        "positions": table_payload(data.get("positions")),
        "orders": table_payload(data.get("orders")),
        "nav_history": table_payload(data.get("nav_history")),
        "decisions": table_payload(data.get("decisions") or []),
        "risk_report": to_browser_value(data.get("risk_report") or {}),
        "execution_diagnostics": to_browser_value(data.get("execution_diagnostics") or {}),
        "trading_settings": to_browser_value(data.get("trading_settings") or {}),
        "order_snapshot_dates": to_browser_value(data.get("order_snapshot_dates") or []),
        "position_snapshot_dates": to_browser_value(data.get("position_snapshot_dates") or []),
        "profile": to_browser_value(data.get("profile") or {}),
        "profile_complete": bool(data.get("profile_complete")),
        "profile_options": to_browser_value(data.get("profile_options") or {}),
        "cash_flows": table_payload(data.get("cash_flows") or []),
        "backfill_status": to_browser_value(data.get("backfill_status") or {}),
        "ai_reliability": to_browser_value(data.get("ai_reliability") or {}),
        "scheduler": to_browser_value(data.get("scheduler") or {}),
        "model_context": to_browser_value(data.get("model_context") or {}),
    }


def present_daily_history(value: dict[str, Any]) -> dict[str, Any]:
    data = dict(value or {})
    return {
        "user_id": str(data.get("user_id") or ""),
        "trade_date": str(data.get("trade_date") or ""),
        "available_dates": to_browser_value(data.get("available_dates") or []),
        "has_position_snapshot": bool(data.get("has_position_snapshot")),
        "positions": table_payload(data.get("positions")),
        "operations": table_payload(data.get("operations")),
        "buy_operations": table_payload(data.get("buy_operations")),
        "sell_operations": table_payload(data.get("sell_operations")),
        "summary": to_browser_value(data.get("summary") or {}),
    }


def present_profile(value: dict[str, Any]) -> dict[str, Any]:
    data = dict(value or {})
    return {
        "user_id": str(data.get("user_id") or ""),
        "profile": to_browser_value(data.get("profile") or {}),
        "complete": bool(data.get("complete")),
        "options": to_browser_value(data.get("options") or {}),
    }


def present_proposals(value: list[dict[str, Any]]) -> dict[str, Any]:
    records = to_browser_value(value or [])
    if not isinstance(records, list):
        records = []
    return {"records": records, "total": len(records)}


def present_write_result(value: dict[str, Any]) -> dict[str, Any]:
    return to_browser_value(value or {})
