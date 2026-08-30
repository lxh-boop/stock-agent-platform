from __future__ import annotations

from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable

from database.schemas import calculate_mapping_confidence, json_dumps, json_loads
from database.postgres_store import PostgresStore


def _parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d %H:%M",
        "%Y-%m-%d",
        "%Y%m%d",
    ]:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(text)


def _normalize_calendar(trading_calendar: Iterable[str | datetime]) -> list[str]:
    dates = []
    for item in trading_calendar:
        dt = _parse_datetime(item)
        dates.append(dt.strftime("%Y-%m-%d"))
    return sorted(set(dates))


def assign_news_trade_date(
    publish_time: str | datetime,
    trading_calendar: Iterable[str | datetime],
    cutoff_time: str | time = "15:00",
) -> str:
    """Assign news to a trading date without using after-close future news."""

    published_at = _parse_datetime(publish_time)
    calendar = _normalize_calendar(trading_calendar)
    if not calendar:
        raise ValueError("trading_calendar cannot be empty")

    if isinstance(cutoff_time, str):
        cutoff = datetime.strptime(cutoff_time, "%H:%M").time()
    else:
        cutoff = cutoff_time

    publish_date = published_at.strftime("%Y-%m-%d")
    if publish_date in calendar and published_at.time() < cutoff:
        return publish_date

    for trade_date in calendar:
        if trade_date > publish_date:
            return trade_date
        if trade_date == publish_date and published_at.time() < cutoff:
            return trade_date

    raise ValueError(f"no trading date available after publish_time={published_at}")


class NewsRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.store = PostgresStore()

    def insert_news_event(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("news_event", record)

    def get_news_event(self, news_id: str) -> dict[str, Any] | None:
        return self.store.get("news_event", {"news_id": news_id})

    def list_news_events(self, trade_date: str | None = None) -> list[dict[str, Any]]:
        filters = {"trade_date": trade_date} if trade_date else None
        return self.store.list("news_event", filters=filters, order_by="publish_time")

    def update_news_event(self, news_id: str, changes: dict[str, Any]) -> int:
        return self.store.update("news_event", {"news_id": news_id}, changes)

    def insert_news_chunk(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("news_chunk", record)

    def get_news_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        return self.store.get("news_chunk", {"chunk_id": chunk_id})

    def list_news_chunks(
        self,
        news_id: str | None = None,
        stock_code: str | None = None,
        trade_date: str | None = None,
    ) -> list[dict[str, Any]]:
        filters = {}
        if news_id:
            filters["news_id"] = news_id
        if stock_code:
            filters["stock_code"] = stock_code
        if trade_date:
            filters["trade_date"] = trade_date
        return self.store.list("news_chunk", filters=filters or None, order_by="chunk_index")

    def increment_chunk_retrieval_count(self, chunk_id: str, count: int = 1) -> int:
        row = self.get_news_chunk(chunk_id)
        if not row:
            return 0
        current = int(row.get("retrieval_count") or 0)
        return self.store.update(
            "news_chunk",
            {"chunk_id": chunk_id},
            {"retrieval_count": current + int(count)},
        )

    def mark_chunk_used_in_decision(self, chunk_id: str, decision_id: str) -> int:
        return self.store.update(
            "news_chunk",
            {"chunk_id": chunk_id},
            {
                "used_in_decision": 1,
                "decision_id": decision_id,
            },
        )

    def insert_news_embedding(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("news_embedding", record)

    def insert_news_stock_mapping(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        if "mapping_confidence" not in payload:
            payload["mapping_confidence"] = calculate_mapping_confidence(
                entity_score=payload.get("entity_score", 0.0),
                event_score=payload.get("event_score", 0.0),
                industry_score=payload.get("industry_score", 0.0),
                source_score=payload.get("source_score", 0.0),
                position_score=payload.get("position_score", 0.0),
                llm_score=payload.get("llm_score", 0.0),
                penalty=payload.get("penalty", 0.0),
            )
        for transient_key in [
            "entity_score",
            "event_score",
            "industry_score",
            "source_score",
            "position_score",
            "llm_score",
            "penalty",
        ]:
            payload.pop(transient_key, None)
        return self.store.upsert("news_stock_mapping", payload)

    def get_news_stock_mapping(self, mapping_id: str) -> dict[str, Any] | None:
        return self.store.get("news_stock_mapping", {"mapping_id": mapping_id})

    def list_news_stock_mappings(
        self,
        news_id: str | None = None,
        stock_code: str | None = None,
    ) -> list[dict[str, Any]]:
        filters = {}
        if news_id:
            filters["news_id"] = news_id
        if stock_code:
            filters["stock_code"] = stock_code
        return self.store.list(
            "news_stock_mapping",
            filters=filters or None,
            order_by="created_at",
        )

    def insert_industry_event_rule(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("industry_event_rule", record)

    def insert_rag_retrieval_log(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        for key in [
            "filters",
            "bm25_results",
            "dense_results",
            "rerank_results",
            "selected_chunk_ids",
            "returned_chunk_ids",
            "used_chunk_ids",
        ]:
            if isinstance(payload.get(key), (list, dict)):
                payload[key] = json_dumps(payload[key])
        return self.store.upsert("rag_retrieval_log", payload)

    def get_rag_retrieval_log(self, retrieval_id: str) -> dict[str, Any] | None:
        row = self.store.get("rag_retrieval_log", {"retrieval_id": retrieval_id})
        if row:
            for key in [
                "filters",
                "bm25_results",
                "dense_results",
                "rerank_results",
                "selected_chunk_ids",
                "returned_chunk_ids",
                "used_chunk_ids",
            ]:
                default = [] if key.endswith("results") or key.endswith("_chunk_ids") else {}
                row[key] = json_loads(row.get(key), default=default)
        return row
