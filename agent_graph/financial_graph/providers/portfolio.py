"""Portfolio-domain bridge between authoritative state and graph snapshots.

Reading the portfolio and materializing a Neo4j snapshot are intentionally
separate atomic capabilities.  The read path never writes graph or business
state.  The materialization path only writes derived graph state and never
changes account cash, positions, orders, strategy bindings, or trading state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_graph.financial_graph.portfolio_graph import PortfolioGraphService


@dataclass
class PortfolioGraphProvider:
    """Portfolio-domain provider operations behind the GraphRef boundary."""

    portfolio_graph: PortfolioGraphService

    def read_portfolio_state(
        self,
        *,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        """Read the authoritative portfolio without creating a graph snapshot."""

        from agent_graph.services.portfolio_service import PortfolioService

        raw = PortfolioService().get_portfolio_state(
            user_id=user_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        if raw.get("success") is False:
            message = str(raw.get("message") or "Portfolio state unavailable.")
            return {
                "success": False,
                "message": message,
                "error_type": str(raw.get("error_type") or "portfolio_snapshot_inconsistent"),
                "error_message": str(raw.get("error_message") or message),
                "failure_kind": str(raw.get("failure_kind") or "business_logic_error"),
                "retryable": bool(raw.get("retryable", False)),
                "warnings": list(raw.get("warnings") or []),
                "errors": list(raw.get("errors") or []),
            }
        if raw.get("found") is False or not raw.get("account"):
            message = str(raw.get("message") or "Portfolio account is not initialized.")
            return {
                "success": False,
                "message": message,
                "error_type": "portfolio_account_not_initialized",
                "error_message": message,
                "failure_kind": "business_result_empty",
                "retryable": False,
                "warnings": list(raw.get("warnings") or []),
                "errors": list(raw.get("errors") or ["missing_account"]),
            }
        return {
            "success": True,
            "message": str(raw.get("message") or "Portfolio state queried."),
            "portfolio": raw,
            "warnings": list(raw.get("warnings") or []),
            "errors": list(raw.get("errors") or []),
        }

    def materialize_portfolio_snapshot(
        self,
        *,
        user_id: str,
        portfolio_state: dict[str, Any],
        as_of_time: str,
        source_task_id: str,
        source_agent_id: str,
    ) -> dict[str, Any]:
        """Write only a derived Neo4j portfolio snapshot from upstream state."""

        raw = dict(portfolio_state or {})
        # W02 exposes a safe, typed read result rather than the provider's raw
        # database payload. Reconstruct the minimal authoritative shape needed
        # for graph materialization from those explicit fields; W08 never
        # re-reads the business database.
        if raw.get("graph_snapshot_materialized") is False:
            totals = raw.get("portfolio_totals") if isinstance(raw.get("portfolio_totals"), dict) else {}
            account = raw.get("account_snapshot") if isinstance(raw.get("account_snapshot"), dict) else {}
            positions: list[dict[str, Any]] = []
            for item in raw.get("display_positions") or []:
                if not isinstance(item, dict):
                    continue
                positions.append(
                    {
                        "stock_code": str(item.get("public_code") or ""),
                        "stock_name": str(item.get("display_label") or ""),
                        "quantity": item.get("quantity"),
                        "cost_price": item.get("cost_price"),
                        "current_price": item.get("current_price"),
                        "market_value": item.get("market_value"),
                        "position_ratio": item.get("position_ratio"),
                        "unrealized_pnl": item.get("unrealized_pnl"),
                        "updated_at": item.get("updated_at"),
                    }
                )
            raw = {
                "success": True,
                "account": account,
                "positions": positions,
                "active_positions": positions,
                "cash": totals.get("cash"),
                "total_assets": totals.get("total_assets"),
                "position_market_value": totals.get("position_market_value"),
                "cash_ratio": totals.get("cash_ratio"),
                "snapshot_id": totals.get("snapshot_id"),
                "as_of_date": raw.get("as_of_time") or totals.get("as_of_date"),
            }
        else:
            nested = raw.get("portfolio_summary")
            if isinstance(nested, dict) and not raw.get("account"):
                raw = dict(nested)
        if raw.get("success") is False:
            message = str(raw.get("message") or "Portfolio state is not safe to materialize.")
            return {
                "success": False,
                "message": message,
                "error_type": str(raw.get("error_type") or "portfolio_state_invalid"),
                "error_message": str(raw.get("error_message") or message),
                "failure_kind": str(raw.get("failure_kind") or "business_logic_error"),
                "retryable": False,
                "warnings": list(raw.get("warnings") or []),
                "errors": list(raw.get("errors") or []),
            }
        if not raw.get("account"):
            message = "Portfolio state does not contain an authoritative account snapshot."
            return {
                "success": False,
                "message": message,
                "error_type": "portfolio_account_not_initialized",
                "error_message": message,
                "failure_kind": "business_result_empty",
                "retryable": False,
                "warnings": [],
                "errors": ["missing_account"],
            }
        ref, graph_result = self.portfolio_graph.upsert_snapshot(
            user_id=user_id,
            portfolio_payload=raw,
            as_of_time=as_of_time,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
        )
        return {
            "success": True,
            "message": "Portfolio graph snapshot materialized.",
            "portfolio_ref": ref.to_dict(),
            "holding_refs": graph_result.get("holding_refs") or [],
            "unresolved_positions": graph_result.get("unresolved_positions") or [],
            "graph_write": graph_result.get("applied") or {},
        }

    def load_portfolio_snapshot(
        self,
        *,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
        as_of_time: str,
        source_task_id: str,
        source_agent_id: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper for older callers.

        New Worker capabilities must call ``read_portfolio_state`` and
        ``materialize_portfolio_snapshot`` independently so MainAgent can choose
        whether a derived graph write is actually needed.
        """

        read_result = self.read_portfolio_state(
            user_id=user_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        if not read_result.get("success"):
            return read_result
        result = self.materialize_portfolio_snapshot(
            user_id=user_id,
            portfolio_state=dict(read_result.get("portfolio") or {}),
            as_of_time=as_of_time,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
        )
        if result.get("success"):
            result["portfolio"] = dict(read_result.get("portfolio") or {})
        return result
