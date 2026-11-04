from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_graph.domain_tools._common import portfolio_user_dir, safe_float
from portfolio.portfolio_snapshot import (
    PortfolioSnapshotConsistencyError,
    build_portfolio_snapshot,
)
from portfolio.storage import PortfolioStorage


def _to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    if isinstance(value, dict):
        return dict(value)
    return {}


def _source(label: str, table: str) -> dict[str, Any]:
    return {
        "label": label,
        "source_type": "database",
        "table": table,
        "exists": True,
    }


def _active_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if safe_float(row.get("quantity"), 0.0) > 0.0]


class AccountRepository:
    def load_account(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> Any:
        storage = PortfolioStorage(db_path, output_dir=portfolio_user_dir(output_dir, user_id))
        return storage.load_account(f"paper_{user_id}")

    def load_historical_account_snapshot(
        self,
        user_id: str,
        trade_date: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        storage = PortfolioStorage(db_path, output_dir=portfolio_user_dir(output_dir, user_id))
        token = "".join(char for char in str(trade_date or "") if char.isdigit())[:8]
        if not token:
            return {}
        history_dir = storage.output_dir / "history" / "accounts"
        candidates = []
        if history_dir.exists():
            candidates = sorted(history_dir.glob(f"account_{token}*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
        return {}


class PortfolioRepository:
    def load_positions(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> list[Any]:
        storage = PortfolioStorage(db_path, output_dir=portfolio_user_dir(output_dir, user_id))
        return storage.load_positions(user_id)


class OrderRepository:
    def load_orders(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> list[Any]:
        storage = PortfolioStorage(db_path, output_dir=portfolio_user_dir(output_dir, user_id))
        return storage.load_orders(user_id)


class PortfolioService:
    def __init__(
        self,
        *,
        account_repository: AccountRepository | None = None,
        portfolio_repository: PortfolioRepository | None = None,
        order_repository: OrderRepository | None = None,
    ) -> None:
        self.account_repository = account_repository or AccountRepository()
        self.portfolio_repository = portfolio_repository or PortfolioRepository()
        self.order_repository = order_repository or OrderRepository()

    def _storage(self, user_id: str, output_dir: str | Path, db_path: str | Path | None) -> PortfolioStorage:
        return PortfolioStorage(db_path, output_dir=portfolio_user_dir(output_dir, user_id))

    def _sources(self, user_id: str, output_dir: str | Path, db_path: str | Path | None) -> list[dict[str, Any]]:
        del user_id, output_dir, db_path
        return [
            _source("paper_account", "paper_account"),
            _source("portfolio_positions", "portfolio_position"),
            _source("paper_orders", "paper_order"),
        ]

    def _snapshot(
        self,
        user_id: str,
        *,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        user = str(user_id or "default")
        account = self.account_repository.load_account(user, output_dir=output_dir, db_path=db_path)
        positions = self.portfolio_repository.load_positions(user, output_dir=output_dir, db_path=db_path)
        sources = self._sources(user, output_dir, db_path)
        try:
            return build_portfolio_snapshot(
                account,
                positions,
                user_id=user,
                account_id=f"paper_{user}",
                sources=sources,
            )
        except PortfolioSnapshotConsistencyError as exc:
            trace = {
                "cash_semantics": "uninvested_cash",
                "cash_used": 0.0,
                "position_components": [],
                "position_market_value_sum": 0.0,
                "total_assets_formula": "uninvested_cash + position_market_value_sum",
                "total_assets": 0.0,
            }
            return {
                "user_id": user,
                "account_id": f"paper_{user}",
                "as_of_date": "",
                "account": {},
                "positions": [],
                "cash": 0.0,
                "position_market_value": 0.0,
                "total_assets": 0.0,
                "cash_ratio": 0.0,
                "cash_semantics": "uninvested_cash",
                "calculation_trace": trace,
                "snapshot_id": "",
                "consistency_status": "rejected",
                "warnings": [],
                "errors": [str(exc.code)],
                "status": "logic_error",
                "error_code": "portfolio_snapshot_inconsistent",
                "safe_to_continue": False,
                "safe_to_answer": False,
                "safe_to_write": False,
                "source_metadata": sources,
                "raw_account_summary": {},
            }

    def get_account_summary(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        user = str(user_id or "default")
        snapshot = self._snapshot(user, output_dir=output_dir, db_path=db_path)
        account_dict = dict(snapshot.get("account") or {})
        summary = {
            "cash": safe_float(snapshot.get("cash"), 0.0),
            "total_assets": safe_float(snapshot.get("total_assets"), 0.0),
            "position_market_value": safe_float(snapshot.get("position_market_value"), 0.0),
            "cash_ratio": safe_float(snapshot.get("cash_ratio"), 0.0),
            "net_contribution": safe_float(account_dict.get("net_contribution"), 0.0),
            "absolute_profit": safe_float(account_dict.get("absolute_profit"), 0.0),
            "time_weighted_return": safe_float(account_dict.get("time_weighted_return"), 0.0),
            "max_drawdown": safe_float(account_dict.get("max_drawdown"), 0.0),
        }
        return {
            "user_id": user,
            "account": account_dict,
            "account_summary": summary,
            "summary": summary,
            "as_of_date": str(snapshot.get("as_of_date") or ""),
            "sources": list(snapshot.get("source_metadata") or []),
            "consistency_status": snapshot.get("consistency_status"),
            "consistency_warnings": list(snapshot.get("warnings") or []),
            "consistency_errors": list(snapshot.get("errors") or []),
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "cash_semantics": str(snapshot.get("cash_semantics") or "uninvested_cash"),
            "calculation_trace": dict(snapshot.get("calculation_trace") or {}),
            "status": str(snapshot.get("status") or "success"),
            "error_code": str(snapshot.get("error_code") or ""),
            "safe_to_continue": bool(snapshot.get("safe_to_continue", snapshot.get("consistency_status") != "rejected")),
            "safe_to_answer": bool(snapshot.get("safe_to_answer", snapshot.get("consistency_status") != "rejected")),
            "safe_to_write": bool(snapshot.get("safe_to_write", snapshot.get("consistency_status") != "rejected")),
            "not_executed": True,
            "mutation_performed": False,
        }

    def get_current_positions(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        user = str(user_id or "default")
        snapshot = self._snapshot(user, output_dir=output_dir, db_path=db_path)
        rows = [dict(item) for item in (snapshot.get("positions") or [])]
        active = _active_positions(rows)
        total_assets = safe_float(snapshot.get("total_assets"), 0.0)
        weights = {
            str(row.get("stock_code") or ""): safe_float(row.get("market_value"), 0.0) / total_assets
            for row in active
            if str(row.get("stock_code") or "") and total_assets > 0
        }
        return {
            "user_id": user,
            "positions": rows,
            "active_positions": active,
            "position_count": len(active),
            "position_market_value": sum(safe_float(row.get("market_value"), 0.0) for row in active),
            "position_weights": weights,
            "as_of_date": str(snapshot.get("as_of_date") or ""),
            "sources": list(snapshot.get("source_metadata") or []),
            "consistency_status": snapshot.get("consistency_status"),
            "consistency_warnings": list(snapshot.get("warnings") or []),
            "consistency_errors": list(snapshot.get("errors") or []),
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "cash_semantics": str(snapshot.get("cash_semantics") or "uninvested_cash"),
            "calculation_trace": dict(snapshot.get("calculation_trace") or {}),
            "portfolio_snapshot": snapshot,
            "status": str(snapshot.get("status") or "success"),
            "error_code": str(snapshot.get("error_code") or ""),
            "safe_to_continue": bool(snapshot.get("safe_to_continue", snapshot.get("consistency_status") != "rejected")),
            "safe_to_answer": bool(snapshot.get("safe_to_answer", snapshot.get("consistency_status") != "rejected")),
            "safe_to_write": bool(snapshot.get("safe_to_write", snapshot.get("consistency_status") != "rejected")),
            "not_executed": True,
            "mutation_performed": False,
        }

    def get_current_orders(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        user = str(user_id or "default")
        rows = [_to_dict(item) for item in self.order_repository.load_orders(user, output_dir=output_dir, db_path=db_path)]
        latest_trade_date = ""
        dates = [str(row.get("trade_date") or "") for row in rows if row.get("trade_date")]
        if dates:
            latest_trade_date = sorted(dates)[-1]
        return {
            "user_id": user,
            "orders": rows,
            "order_count": len(rows),
            "latest_trade_date": latest_trade_date,
            "sources": self._sources(user, output_dir, db_path),
            "not_executed": True,
            "mutation_performed": False,
        }

    def get_cash_state(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        snapshot = self._snapshot(str(user_id or "default"), output_dir=output_dir, db_path=db_path)
        return {
            "user_id": str(user_id or "default"),
            "cash_state": {
                "cash": snapshot.get("cash", 0.0),
                "cash_ratio": snapshot.get("cash_ratio", 0.0),
                "total_assets": snapshot.get("total_assets", 0.0),
                "position_market_value": snapshot.get("position_market_value", 0.0),
            },
            "consistency_status": snapshot.get("consistency_status"),
            "not_executed": True,
            "mutation_performed": False,
        }

    def get_position_weights(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        user = str(user_id or "default")
        snapshot = self._snapshot(user, output_dir=output_dir, db_path=db_path)
        positions = [dict(item) for item in (snapshot.get("positions") or [])]
        total_assets = safe_float(snapshot.get("total_assets"), 0.0)
        weights = {
            str(row.get("stock_code") or ""): safe_float(row.get("market_value"), 0.0) / total_assets
            for row in positions
            if safe_float(row.get("quantity"), 0.0) > 0.0 and str(row.get("stock_code") or "") and total_assets > 0
        }
        return {
            "user_id": user,
            "position_weights": weights,
            "max_single_weight": max(weights.values(), default=0.0),
            "consistency_status": snapshot.get("consistency_status"),
            "not_executed": True,
            "mutation_performed": False,
        }

    def get_historical_account_snapshot(
        self,
        user_id: str,
        trade_date: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        snapshot = self.account_repository.load_historical_account_snapshot(
            str(user_id or "default"),
            trade_date,
            output_dir=output_dir,
            db_path=db_path,
        )
        return {
            "user_id": str(user_id or "default"),
            "trade_date": str(trade_date or ""),
            "account_snapshot": snapshot,
            "not_executed": True,
            "mutation_performed": False,
        }

    def get_portfolio_state(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        user = str(user_id or "default")
        snapshot = self._snapshot(user, output_dir=output_dir, db_path=db_path)
        account_dict = dict(snapshot.get("account") or {})
        position_rows = [dict(item) for item in (snapshot.get("positions") or [])]
        active_positions = _active_positions(position_rows)
        orders = self.get_current_orders(user, output_dir=output_dir, db_path=db_path)
        total_assets = safe_float(snapshot.get("total_assets"), 0.0)
        weights = {
            str(row.get("stock_code") or ""): safe_float(row.get("market_value"), 0.0) / total_assets
            for row in active_positions
            if str(row.get("stock_code") or "") and total_assets > 0
        }
        account_summary = {
            "cash": safe_float(snapshot.get("cash"), 0.0),
            "total_assets": total_assets,
            "position_market_value": safe_float(snapshot.get("position_market_value"), 0.0),
            "cash_ratio": safe_float(snapshot.get("cash_ratio"), 0.0),
            "net_contribution": safe_float(account_dict.get("net_contribution"), 0.0),
            "absolute_profit": safe_float(account_dict.get("absolute_profit"), 0.0),
            "time_weighted_return": safe_float(account_dict.get("time_weighted_return"), 0.0),
            "max_drawdown": safe_float(account_dict.get("max_drawdown"), 0.0),
        }
        summary = {
            "position_count": len(active_positions),
            "order_count": orders["order_count"],
            "cash": account_summary["cash"],
            "total_assets": account_summary["total_assets"],
            "position_market_value": account_summary["position_market_value"],
            "cash_ratio": account_summary["cash_ratio"],
        }
        consistency_status = str(snapshot.get("consistency_status") or "")
        snapshot_status = str(snapshot.get("status") or "success")
        found = bool(account_dict)
        safe_to_answer = bool(
            snapshot.get(
                "safe_to_answer",
                consistency_status not in {"rejected"} and snapshot_status != "logic_error",
            )
        )
        success = bool(safe_to_answer and snapshot_status != "logic_error")
        error_type = ""
        failure_kind = ""
        message = "Portfolio state queried."
        if not success:
            error_type = str(snapshot.get("error_code") or "portfolio_snapshot_inconsistent")
            failure_kind = "business_logic_error"
            message = "Portfolio snapshot is inconsistent and cannot be used safely."
        elif not found:
            message = "Portfolio account is not initialized."

        return {
            "success": success,
            "found": found,
            "message": message,
            "error_type": error_type,
            "error_message": message if not success else "",
            "failure_kind": failure_kind,
            "retryable": False,
            "warnings": list(snapshot.get("warnings") or []),
            "errors": list(snapshot.get("errors") or []),
            "user_id": user,
            "account": account_dict,
            "account_summary": account_summary,
            "positions": position_rows,
            "active_positions": active_positions,
            "orders": orders["orders"],
            "position_count": len(active_positions),
            "order_count": orders["order_count"],
            "cash": summary["cash"],
            "total_assets": summary["total_assets"],
            "cash_state": {
                "cash": summary["cash"],
                "cash_ratio": summary["cash_ratio"],
                "total_assets": summary["total_assets"],
                "position_market_value": summary["position_market_value"],
            },
            "position_weights": weights,
            "summary": summary,
            "as_of_date": str(snapshot.get("as_of_date") or orders.get("latest_trade_date") or ""),
            "sources": list(snapshot.get("source_metadata") or []),
            "consistency_status": snapshot.get("consistency_status"),
            "consistency_warnings": list(snapshot.get("warnings") or []),
            "consistency_errors": list(snapshot.get("errors") or []),
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "cash_semantics": str(snapshot.get("cash_semantics") or "uninvested_cash"),
            "calculation_trace": dict(snapshot.get("calculation_trace") or {}),
            "portfolio_snapshot": snapshot,
            "status": str(snapshot.get("status") or "success"),
            "error_code": str(snapshot.get("error_code") or ""),
            "safe_to_continue": bool(snapshot.get("safe_to_continue", snapshot.get("consistency_status") != "rejected")),
            "safe_to_answer": bool(snapshot.get("safe_to_answer", snapshot.get("consistency_status") != "rejected")),
            "safe_to_write": bool(snapshot.get("safe_to_write", snapshot.get("consistency_status") != "rejected")),
            "not_executed": True,
            "mutation_performed": False,
        }


portfolio_service = PortfolioService()
