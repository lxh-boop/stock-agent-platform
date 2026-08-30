from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from database.repositories.news_repository import assign_news_trade_date
from database.postgres_store import PostgresStore
from database.table_registry import primary_key_for
from event_rules import classify_event_title
from news_data import EVENT_COLUMNS, load_event_cache, refresh_news_event_cache
from rag.chunkers import chunk_announcement, chunk_news


POSITIVE_EVENT_TYPES = {
    "earnings_positive",
    "shareholder_increase",
    "merger",
    "buyback",
    "contract_win",
}
NEGATIVE_EVENT_TYPES = {
    "earnings_negative",
    "shareholder_reduce",
    "lawsuit",
    "penalty",
    "risk",
}


@dataclass(frozen=True)
class NewsSyncResult:
    input_rows: int = 0
    filtered_rows: int = 0
    event_rows: int = 0
    chunk_rows: int = 0
    mapping_rows: int = 0
    positive_mappings: int = 0
    negative_mappings: int = 0
    neutral_mappings: int = 0
    cache_rows: int = 0
    fetch_status: dict[str, Any] | None = None
    chunk_statistics: dict[str, Any] | None = None
    db_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fetch_status"] = self.fetch_status or {}
        data["chunk_statistics"] = self.chunk_statistics or {}
        return data


def _stable_id(prefix: str, *parts: Any) -> str:
    text = "\n".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:20]}"


def _date_text(value: Any) -> str:
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return ""
    return dt.strftime("%Y-%m-%d")


def _time_text(value: Any, fallback_date: Any = "") -> str:
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        date_text = _date_text(fallback_date)
        return f"{date_text} 00:00:00" if date_text else ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return " ".join(text.split())


def classify_content_level(title: Any, summary: Any, content: Any) -> str:
    title_text = _clean_text(title)
    summary_text = _clean_text(summary)
    content_text = _clean_text(content)
    if content_text and content_text != title_text and content_text != summary_text:
        return "full_text"
    if content_text and content_text == summary_text and content_text != title_text:
        return "summary"
    if summary_text and summary_text != title_text:
        return "summary"
    return "title_only"


def _chunk_statistics(
    *,
    events: list[dict[str, Any]],
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    if not events and not chunks:
        return {
            "event_count": 0,
            "chunk_count": 0,
            "event_to_chunk_distribution": {},
            "event_chunk_distribution": {},
            "char_length_percentiles": {},
            "text_length_p50": 0.0,
            "text_length_p90": 0.0,
            "text_length_p95": 0.0,
            "text_length_p99": 0.0,
            "empty_content_count": 0,
            "empty_chunk_count": 0,
            "oversized_chunk_count": 0,
            "duplicate_chunk_count": 0,
            "future_news_filtered_count": 0,
            "stock_distribution": {},
            "stock_news_distribution": {},
            "source_distribution": {},
            "content_level_distribution": {},
        }
    event_chunk_counts: dict[str, int] = {}
    for chunk in chunks:
        news_id = str(chunk.get("news_id") or "")
        event_chunk_counts[news_id] = event_chunk_counts.get(news_id, 0) + 1
    count_values = list(event_chunk_counts.values())
    char_lengths = [len(str(chunk.get("chunk_text") or "")) for chunk in chunks]
    length_series = pd.Series(char_lengths, dtype="float64") if char_lengths else pd.Series([], dtype="float64")
    def counts(values: list[Any]) -> dict[str, int]:
        series = pd.Series(values).fillna("").astype(str)
        return {str(key): int(value) for key, value in series.value_counts().sort_index().items()}

    duplicate_count = 0
    if chunks:
        frame = pd.DataFrame(
            [
                {
                    "news_id": chunk.get("news_id", ""),
                    "stock_code": chunk.get("stock_code", ""),
                    "chunk_index": chunk.get("chunk_index", ""),
                    "chunk_text": chunk.get("chunk_text", ""),
                }
                for chunk in chunks
            ]
        )
        duplicate_count = int(
            (
                frame.duplicated(subset=["news_id", "chunk_index"], keep=False)
                | frame.duplicated(subset=["news_id", "stock_code", "chunk_text"], keep=False)
            ).sum()
        )
    length_percentiles = {
        "p50": float(length_series.quantile(0.50)) if not length_series.empty else 0.0,
        "p90": float(length_series.quantile(0.90)) if not length_series.empty else 0.0,
        "p95": float(length_series.quantile(0.95)) if not length_series.empty else 0.0,
        "p99": float(length_series.quantile(0.99)) if not length_series.empty else 0.0,
    }
    event_distribution = {
        "min": int(min(count_values or [0])),
        "max": int(max(count_values or [0])),
        "avg": float(sum(count_values) / len(count_values)) if count_values else 0.0,
        "single_chunk_events": int(sum(1 for value in count_values if value == 1)),
        "multi_chunk_events": int(sum(1 for value in count_values if value > 1)),
    }
    stock_distribution = counts([event.get("stock_code") for event in events])
    empty_content_count = int(
        sum(
            1
            for event in events
            if not _clean_text(event.get("summary"))
            and not _clean_text(event.get("content"))
            and str(event.get("content_level") or "title_only") == "title_only"
        )
    )
    return {
        "event_count": len(events),
        "chunk_count": len(chunks),
        "event_to_chunk_distribution": event_distribution,
        "event_chunk_distribution": event_distribution,
        "char_length_percentiles": length_percentiles,
        "text_length_p50": length_percentiles["p50"],
        "text_length_p90": length_percentiles["p90"],
        "text_length_p95": length_percentiles["p95"],
        "text_length_p99": length_percentiles["p99"],
        "empty_content_count": empty_content_count,
        "empty_chunk_count": int(sum(1 for length in char_lengths if length == 0)),
        "oversized_chunk_count": int(sum(1 for length in char_lengths if length > 1200)),
        "duplicate_chunk_count": duplicate_count,
        "future_news_filtered_count": 0,
        "stock_distribution": stock_distribution,
        "stock_news_distribution": stock_distribution,
        "source_distribution": counts([event.get("source") for event in events]),
        "content_level_distribution": counts([event.get("content_level") or "title_only" for event in events]),
    }


def _trading_calendar(events: pd.DataFrame, output_dir: str | Path = "outputs") -> list[str]:
    dates: set[str] = set()
    for rel in [
        Path("data") / "latest_raw_stock_data.csv",
        Path("data") / "raw_stock_data.csv",
        Path(output_dir) / "ranking_latest.csv",
    ]:
        if not rel.exists():
            continue
        try:
            header = pd.read_csv(rel, nrows=0, encoding="utf-8-sig")
            date_col = "date" if "date" in header.columns else "trade_date" if "trade_date" in header.columns else None
            if date_col is None:
                continue
            data = pd.read_csv(rel, usecols=[date_col], encoding="utf-8-sig")
            dates.update(_date_text(item) for item in data[date_col].dropna().unique())
        except Exception:
            continue

    if "date" in events.columns:
        event_dates = pd.to_datetime(events["date"], errors="coerce").dropna()
        if not event_dates.empty:
            start = event_dates.min() - pd.Timedelta(days=7)
            end = event_dates.max() + pd.Timedelta(days=14)
            dates.update(dt.strftime("%Y-%m-%d") for dt in pd.bdate_range(start=start, end=end))

    return sorted(date for date in dates if date)


def _event_classification(title: str) -> tuple[str, str, float, float, float, int]:
    flags = classify_event_title(title)
    if flags.get("has_earnings_positive"):
        event_type = "earnings_positive"
    elif flags.get("has_earnings_negative"):
        event_type = "earnings_negative"
    elif flags.get("has_shareholder_reduce"):
        event_type = "shareholder_reduce"
    elif flags.get("has_shareholder_increase"):
        event_type = "shareholder_increase"
    elif flags.get("has_lawsuit"):
        event_type = "lawsuit"
    elif flags.get("has_penalty"):
        event_type = "penalty"
    elif flags.get("has_merger"):
        event_type = "merger"
    elif flags.get("has_buyback"):
        event_type = "buyback"
    elif flags.get("has_contract_win"):
        event_type = "contract_win"
    elif flags.get("is_risk_event"):
        event_type = "risk"
    elif flags.get("is_positive_event"):
        event_type = "positive"
    elif flags.get("is_negative_event"):
        event_type = "negative"
    else:
        event_type = "neutral"

    if event_type in POSITIVE_EVENT_TYPES or event_type == "positive":
        direction = "positive"
        sentiment = "positive"
        strength = 0.75
    elif event_type in NEGATIVE_EVENT_TYPES or event_type == "negative":
        direction = "negative"
        sentiment = "negative"
        strength = 0.80 if event_type in {"risk", "penalty", "lawsuit"} else 0.75
    else:
        direction = "neutral"
        sentiment = "neutral"
        strength = 0.0

    is_major = int(direction != "neutral" and event_type in POSITIVE_EVENT_TYPES | NEGATIVE_EVENT_TYPES)
    importance = 0.80 if is_major else 0.50
    impact_confidence = 0.80 if direction != "neutral" else 0.0
    return event_type, sentiment, strength, impact_confidence, importance, is_major


def _is_announcement(source: str) -> int:
    text = str(source or "").lower()
    return int(any(token in text for token in ["ann", "notice", "announcement", "公告"]))


def _bulk_upsert(db_path: str | Path | None, table: str, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    _ = db_path
    store = PostgresStore()
    with store.transaction() as conn:
        for record in records:
            store.upsert(table, record, connection=conn)


def _delete_chunks_for_news_ids(db_path: str | Path | None, news_ids: list[str]) -> None:
    ids = [str(item or "") for item in news_ids if str(item or "").strip()]
    if not ids:
        return
    _ = db_path
    store = PostgresStore()
    with store.transaction() as conn:
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ", ".join("%s" for _ in batch)
            conn.execute(
                f"DELETE FROM news_chunk WHERE news_id IN ({placeholders})",
                batch,
            )


def _protected_full_text_news_ids(
    db_path: str | Path | None,
    incoming_events: list[dict[str, Any]],
) -> set[str]:
    ids = [
        str(record.get("news_id") or "")
        for record in incoming_events
        if str(record.get("news_id") or "").strip()
        and str(record.get("content_level") or "title_only") != "full_text"
    ]
    if not ids:
        return set()
    _ = db_path
    protected: set[str] = set()
    with PostgresStore().transaction() as conn:
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ", ".join("%s" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT news_id, content
                FROM news_event
                WHERE news_id IN ({placeholders})
                  AND COALESCE(content_level, 'title_only') = 'full_text'
                  AND LENGTH(COALESCE(content, '')) > 0
                """,
                batch,
            ).fetchall()
            protected.update(str(row["news_id"]) for row in rows)
    return protected


def _filter_by_date(events: pd.DataFrame, start_date: str | None, end_date: str | None) -> pd.DataFrame:
    if events.empty:
        return events
    data = events.copy()
    dates = pd.to_datetime(data["date"], errors="coerce")
    if start_date:
        dates_start = pd.to_datetime(start_date, errors="coerce")
        if not pd.isna(dates_start):
            data = data[dates >= dates_start].copy()
            dates = pd.to_datetime(data["date"], errors="coerce")
    if end_date:
        dates_end = pd.to_datetime(end_date, errors="coerce")
        if not pd.isna(dates_end):
            data = data[dates <= dates_end].copy()
    return data.reset_index(drop=True)


def sync_event_cache_to_agent_db(
    stock_pool: dict | None = None,
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
    start_date: str | None = None,
    end_date: str | None = None,
    events: pd.DataFrame | None = None,
    full_text_only: bool = False,
    allow_legacy_cache_sync: bool = False,
) -> NewsSyncResult:
    raw_events = events if events is not None else load_event_cache(stock_pool=stock_pool)
    if events is None and not allow_legacy_cache_sync:
        return NewsSyncResult(
            input_rows=0 if raw_events is None else len(raw_events),
            filtered_rows=0,
            fetch_status={
                "status": "skipped",
                "reason": "legacy_cache_sync_disabled_fulltext_first",
            },
            db_path=str(db_path or ""),
        )
    if raw_events is None or raw_events.empty:
        return NewsSyncResult(db_path=str(db_path or ""), input_rows=0)

    data = raw_events.copy()
    for col in EVENT_COLUMNS:
        if col not in data.columns:
            data[col] = ""
    data = _filter_by_date(data, start_date=start_date, end_date=end_date)
    if data.empty:
        return NewsSyncResult(input_rows=len(raw_events), db_path=str(db_path or ""))

    data["code"] = data["code"].astype(str).str.extract(r"(\d{6})")[0]
    data = data.dropna(subset=["code"]).copy()
    data["code"] = data["code"].astype(str).str.zfill(6)
    data["title"] = data["title"].fillna("").astype(str).str.strip()
    data = data[data["title"].ne("")].copy()
    data = data.drop_duplicates(subset=["date", "code", "title"], keep="last")
    if full_text_only:
        levels = [
            classify_content_level(row.get("title"), row.get("summary"), row.get("content"))
            for row in data.to_dict(orient="records")
        ]
        data = data.assign(_content_level=levels)
        data = data[data["_content_level"].eq("full_text")].drop(columns=["_content_level"]).copy()
        if data.empty:
            return NewsSyncResult(
                input_rows=len(raw_events),
                filtered_rows=0,
                cache_rows=len(raw_events),
                fetch_status={
                    "status": "business_empty",
                    "reason": "no_validated_full_text_rows",
                },
                db_path=str(db_path or ""),
            )

    calendar = _trading_calendar(data, output_dir=output_dir)
    event_rows = 0
    chunk_rows = 0
    mapping_rows = 0
    positive = 0
    negative = 0
    neutral = 0
    event_records: list[dict[str, Any]] = []
    chunk_records: list[dict[str, Any]] = []
    mapping_records: list[dict[str, Any]] = []

    for row in data.to_dict(orient="records"):
        code = str(row.get("code") or "").zfill(6)
        title = str(row.get("title") or "").strip()
        name = str(row.get("name") or (stock_pool or {}).get(code, "") or "")
        source = str(row.get("source") or "")
        url = str(row.get("url") or "")
        publish_time = _time_text(row.get("publish_time"), fallback_date=row.get("date"))
        trade_date = _date_text(row.get("date"))
        if calendar and publish_time:
            try:
                trade_date = assign_news_trade_date(publish_time, calendar)
            except Exception:
                pass

        summary = _clean_text(row.get("summary"))
        content = _clean_text(row.get("content"))
        content_level = classify_content_level(title, summary, content)
        chunk_content = content or summary or title
        news_id = _stable_id("news", row.get("date"), code, title, source, url)
        mapping_id = _stable_id("mapping", news_id, code, "direct_cache")
        content_hash = _stable_id("hash", title, summary, content, source, url)
        event_type, sentiment, strength, impact_confidence, importance, is_major = _event_classification(title)
        direction = "positive" if sentiment == "positive" else "negative" if sentiment == "negative" else "neutral"
        is_announcement = _is_announcement(source)
        retention_level = "hot" if direction != "neutral" or is_announcement else "warm"
        mapping_confidence = 0.90 if is_announcement else 0.75
        relevance_score = 1.0 if is_announcement else 0.80

        event_records.append(
            {
                "news_id": news_id,
                "title": title,
                "summary": summary,
                "content": content,
                "content_level": content_level,
                "raw_file_path": "",
                "archive_file_path": "",
                "source": source,
                "publish_time": publish_time,
                "trade_date": trade_date,
                "event_type": event_type,
                "sentiment": sentiment,
                "importance_score": importance,
                "is_announcement": is_announcement,
                "url": url,
                "content_hash": content_hash,
                "retention_level": retention_level,
                "is_major_event": is_major,
                "is_used_by_agent": 0,
                "raw_content_saved": 0,
                "expire_at": "",
            }
        )
        event_rows += 1

        chunk_input = {
            "news_id": news_id,
            "title": title,
            "summary": summary,
            "content": chunk_content,
            "content_level": content_level,
            "source": source,
            "publish_time": publish_time,
            "trade_date": trade_date,
            "stock_codes": [code],
            "stock_code": code,
            "industry": "",
            "event_type": event_type,
            "is_announcement": bool(is_announcement),
            "url": url,
            "importance_score": importance,
            "retention_level": retention_level,
            "metadata": {"stock_name": name},
        }
        chunks = chunk_announcement(chunk_input) if is_announcement else chunk_news(chunk_input)
        for chunk in chunks:
            record = chunk.to_database_record()
            record.update(
                {
                    "used_in_decision": 0,
                    "retrieval_count": 0,
                    "expire_at": "",
                }
            )
            chunk_records.append(record)
            chunk_rows += 1

        mapping_records.append(
            {
                "mapping_id": mapping_id,
                "news_id": news_id,
                "stock_code": code,
                "stock_name": name,
                "industry": "",
                "concept": "",
                "relevance_score": relevance_score,
                "impact_direction": direction,
                "impact_strength": strength,
                "impact_confidence": impact_confidence,
                "mapping_confidence": mapping_confidence,
                "mapping_method": "direct_cache",
                "evidence_text": summary or content or title,
            }
        )
        mapping_rows += 1
        if direction == "positive":
            positive += 1
        elif direction == "negative":
            negative += 1
        else:
            neutral += 1

    protected_news_ids = _protected_full_text_news_ids(db_path, event_records)
    event_records_to_write = [
        record for record in event_records if record["news_id"] not in protected_news_ids
    ]
    chunk_records_to_write = [
        record for record in chunk_records if record["news_id"] not in protected_news_ids
    ]
    _bulk_upsert(db_path, "news_event", event_records_to_write)
    _delete_chunks_for_news_ids(db_path, [record["news_id"] for record in event_records_to_write])
    _bulk_upsert(db_path, "news_chunk", chunk_records_to_write)
    _bulk_upsert(db_path, "news_stock_mapping", mapping_records)

    stats_events = [
        {
            "news_id": record["news_id"],
            "stock_code": mapping.get("stock_code", ""),
            "source": record.get("source", ""),
            "content_level": record.get("content_level", "title_only"),
            "summary": record.get("summary", ""),
            "content": record.get("content", ""),
        }
        for record, mapping in zip(event_records, mapping_records)
    ]
    stats = _chunk_statistics(events=stats_events, chunks=chunk_records)

    return NewsSyncResult(
        input_rows=len(raw_events),
        filtered_rows=len(data),
        event_rows=event_rows,
        chunk_rows=chunk_rows,
        mapping_rows=mapping_rows,
        positive_mappings=positive,
        negative_mappings=negative,
        neutral_mappings=neutral,
        cache_rows=len(raw_events),
        chunk_statistics=stats,
        db_path=str(db_path or ""),
    )


def refresh_and_sync_news_to_agent_db(
    token: str | None,
    stock_pool: dict | None,
    start_date: str,
    end_date: str,
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> NewsSyncResult:
    events, status = refresh_news_event_cache(
        token=token,
        stock_pool=stock_pool,
        start_date=start_date,
        end_date=end_date,
    )
    result = sync_event_cache_to_agent_db(
        stock_pool=stock_pool,
        db_path=db_path,
        output_dir=output_dir,
        start_date=start_date,
        end_date=end_date,
        events=events,
    )
    return NewsSyncResult(**{**result.to_dict(), "fetch_status": status})
