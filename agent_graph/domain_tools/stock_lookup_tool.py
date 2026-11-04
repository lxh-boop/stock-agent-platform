from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.domain_tools._common import first_present, normalize_stock_code, safe_int


def load_latest_ranking(
    output_dir: str | Path = "outputs",
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    from database.repositories import PredictionRepository

    del output_dir
    return PredictionRepository(db_path).list_latest_predictions()


def load_latest_recommendations(
    user_id: str = "default",
    output_dir: str | Path = "outputs",
    *,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    from database.repositories import RecommendationRepository

    del output_dir
    return RecommendationRepository(db_path).list_latest(user_id)


def _row_code(row: dict[str, Any]) -> str:
    return normalize_stock_code(first_present(row, ["stock_code", "code", "ts_code"], ""))


def _row_name(row: dict[str, Any]) -> str:
    return str(first_present(row, ["stock_name", "name", "asset_name"], ""))


def find_stock_row(records: list[dict[str, Any]], stock_query: str) -> dict[str, Any] | None:
    query = str(stock_query or "").strip()
    query_code = normalize_stock_code(query)
    if query_code:
        for row in records:
            if _row_code(row) == query_code:
                return row
    if query:
        for row in records:
            if query in _row_name(row):
                return row
    return None


def lookup_stock(
    stock_query: str,
    user_id: str = "default",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    # Compatibility wrapper. Agent default path is market.lookup_stock via ToolExecutor.
    # planned_removal_phase=post_phase11_1_legacy_cleanup
    from agent_graph.services.market_analysis_service import market_analysis_service

    return market_analysis_service.lookup_stock(
        stock_query,
        user_id=user_id,
        output_dir=output_dir,
        db_path=db_path,
    )
