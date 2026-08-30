from __future__ import annotations

from pathlib import Path
from typing import Any

from database.postgres_store import PostgresStore


class EvaluationRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.store = PostgresStore()

    def insert_backtest_evaluation(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("backtest_evaluation", record)

    def get_backtest_evaluation(self, eval_id: str) -> dict[str, Any] | None:
        return self.store.get("backtest_evaluation", {"eval_id": eval_id})

    def list_backtest_evaluations(
        self,
        strategy_name: str | None = None,
    ) -> list[dict[str, Any]]:
        filters = {"strategy_name": strategy_name} if strategy_name else None
        return self.store.list("backtest_evaluation", filters=filters, order_by="created_at")

    def update_backtest_evaluation(self, eval_id: str, changes: dict[str, Any]) -> int:
        return self.store.update("backtest_evaluation", {"eval_id": eval_id}, changes)
