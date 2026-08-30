from __future__ import annotations

from pathlib import Path
from typing import Any

from database.schemas import json_dumps, json_loads
from database.postgres_store import PostgresStore


class StockRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.store = PostgresStore()

    def insert_stock_basic(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        if isinstance(payload.get("concepts"), (list, dict)):
            payload["concepts"] = json_dumps(payload["concepts"])
        return self.store.upsert("stock_basic", payload)

    def get_stock_basic(self, stock_code: str) -> dict[str, Any] | None:
        row = self.store.get("stock_basic", {"stock_code": stock_code})
        if row:
            row["concepts"] = json_loads(row.get("concepts"), default=[])
        return row

    def list_stock_basic(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self.store.list("stock_basic", order_by="stock_code", limit=limit)
        for row in rows:
            row["concepts"] = json_loads(row.get("concepts"), default=[])
        return rows

    def insert_stock_alias(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("stock_alias", record)

    def list_aliases(self, stock_code: str | None = None) -> list[dict[str, Any]]:
        filters = {"stock_code": stock_code} if stock_code else None
        return self.store.list("stock_alias", filters=filters, order_by="alias_name")

    def insert_market_data_daily(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("market_data_daily", record)

    def get_market_data_daily(self, trade_date: str, stock_code: str) -> dict[str, Any] | None:
        return self.store.get(
            "market_data_daily",
            {"trade_date": trade_date, "stock_code": stock_code},
        )
