from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.market_analysis_service import market_analysis_service
from agent_graph.services.portfolio_service import portfolio_service
from agent_graph.services.user_profile_service import user_profile_service
from database.repositories import RecommendationRepository, StockRepository


def _stock_code(value: Any) -> str:
    text = str(value or "").strip().split(".")[0]
    return text.zfill(6) if text.isdigit() else text


class DataQueryService:
    """Read-only application service exposed by the internal Data MCP adapter."""

    def get_user_profile(
        self,
        user_id: str,
        *,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        return user_profile_service.get_user_profile(
            str(user_id or "default"),
            output_dir=output_dir,
            db_path=db_path,
        )

    def get_portfolio_state(
        self,
        user_id: str,
        *,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        return portfolio_service.get_portfolio_state(
            str(user_id or "default"),
            output_dir=output_dir,
            db_path=db_path,
        )

    def get_positions(
        self,
        user_id: str,
        *,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        return portfolio_service.get_current_positions(
            str(user_id or "default"),
            output_dir=output_dir,
            db_path=db_path,
        )

    def get_orders(
        self,
        user_id: str,
        *,
        limit: int,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        result = portfolio_service.get_current_orders(
            str(user_id or "default"),
            output_dir=output_dir,
            db_path=db_path,
        )
        rows = list(result.get("orders") or [])
        result["orders"] = rows[-max(1, min(int(limit or 200), 1000)) :]
        result["returned_count"] = len(result["orders"])
        return result

    def get_stock_info(
        self,
        stock_code: str,
        *,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        code = _stock_code(stock_code)
        basic = StockRepository(db_path).get_stock_basic(code) or {}
        market = market_analysis_service.lookup_stock(
            code,
            user_id=str(user_id or "default"),
            output_dir=output_dir,
            db_path=db_path,
        )
        return {
            "success": bool(basic or market.get("success")),
            "stock_code": code,
            "stock_basic": basic,
            "market_snapshot": market,
            "source": "database",
        }

    def get_latest_ranking(
        self,
        *,
        top_k: int,
        model_name: str,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        return market_analysis_service.get_ranking(
            top_k=max(1, min(int(top_k or 10), 500)),
            model_name=str(model_name or "") or None,
            output_dir=output_dir,
            db_path=db_path,
        )

    def get_latest_recommendations(
        self,
        user_id: str,
        *,
        top_k: int,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        rows = RecommendationRepository(db_path).list_latest(
            str(user_id or "default")
        )
        limit = max(1, min(int(top_k or 50), 500))
        return {
            "success": bool(rows),
            "user_id": str(user_id or "default"),
            "records": rows[:limit],
            "total_count": len(rows),
            "returned_count": min(len(rows), limit),
            "source": "database/portfolio_recommendation_result",
        }


data_query_service = DataQueryService()
