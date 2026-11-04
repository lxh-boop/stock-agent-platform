from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.data_query_service import data_query_service


class DataMCPAdapter:
    """Protocol-neutral adapter between MCP tools and application services."""

    def __init__(
        self,
        *,
        db_path: str | Path | None,
        output_dir: str | Path,
    ) -> None:
        self.db_path = db_path
        self.output_dir = Path(output_dir)

    @staticmethod
    def _result(data: dict[str, Any], message: str) -> dict[str, Any]:
        success = bool(data.get("success", True))
        nested = data.get("data")
        payload = dict(nested) if isinstance(nested, dict) else dict(data)
        return {
            "success": success,
            "message": str(data.get("message") or message),
            "data": payload,
            "warnings": list(data.get("warnings") or []),
            "errors": list(data.get("errors") or []),
        }

    def get_user_profile(self, user_id: str) -> dict[str, Any]:
        return self._result(
            data_query_service.get_user_profile(
                user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            ),
            "User profile queried through Data MCP.",
        )

    def get_portfolio_state(self, user_id: str) -> dict[str, Any]:
        return self._result(
            data_query_service.get_portfolio_state(
                user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            ),
            "Portfolio state queried through Data MCP.",
        )

    def get_positions(self, user_id: str) -> dict[str, Any]:
        return self._result(
            data_query_service.get_positions(
                user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            ),
            "Positions queried through Data MCP.",
        )

    def get_orders(self, user_id: str, limit: int = 200) -> dict[str, Any]:
        return self._result(
            data_query_service.get_orders(
                user_id,
                limit=limit,
                output_dir=self.output_dir,
                db_path=self.db_path,
            ),
            "Orders queried through Data MCP.",
        )

    def get_stock_info(
        self,
        stock_code: str,
        user_id: str = "default",
    ) -> dict[str, Any]:
        return self._result(
            data_query_service.get_stock_info(
                stock_code,
                user_id=user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            ),
            "Stock information queried through Data MCP.",
        )

    def get_latest_ranking(
        self,
        top_k: int = 10,
        model_name: str = "",
    ) -> dict[str, Any]:
        return self._result(
            data_query_service.get_latest_ranking(
                top_k=top_k,
                model_name=model_name,
                output_dir=self.output_dir,
                db_path=self.db_path,
            ),
            "Latest ranking queried through Data MCP.",
        )

    def get_latest_recommendations(
        self,
        user_id: str,
        top_k: int = 50,
    ) -> dict[str, Any]:
        return self._result(
            data_query_service.get_latest_recommendations(
                user_id,
                top_k=top_k,
                db_path=self.db_path,
            ),
            "Latest recommendations queried through Data MCP.",
        )
