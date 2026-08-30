from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from local_config import load_local_config
from news_data import load_event_cache, refresh_news_event_cache
from universe import get_stock_pool
from .schema import NEWS_MAPPING_DB_PATH, execute_many, get_connection, init_db


NEWS_COLUMNS = [
    "news_id",
    "date",
    "publish_time",
    "title",
    "content",
    "source",
    "url",
    "raw_text_hash",
    "created_at",
]


def normalize_text(value) -> str:
    return str(value or "").strip()


def stable_hash(*parts: str) -> str:
    text = "\n".join(normalize_text(p) for p in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def normalize_news_dataframe(df: pd.DataFrame, source: str = "") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=NEWS_COLUMNS)

    data = df.copy()

    title_col = first_existing_column(
        data,
        ["title", "ann_title", "news_title", "content", "summary"],
    )
    content_col = first_existing_column(
        data,
        ["content", "body", "summary", "title", "ann_title", "news_title"],
    )
    source_col = first_existing_column(data, ["source", "src", "channel"])
    url_col = first_existing_column(data, ["url", "pdf_url", "link"])
    date_col = first_existing_column(
        data,
        ["date", "ann_date", "trade_date", "publish_date", "pub_date"],
    )
    time_col = first_existing_column(
        data,
        ["publish_time", "datetime", "pub_time", "ann_time", "time"],
    )

    out = pd.DataFrame()
    out["title"] = data[title_col].astype(str) if title_col else ""
    out["content"] = data[content_col].astype(str) if content_col else out["title"]
    out["content"] = out["content"].where(out["content"].str.strip().ne(""), out["title"])

    if source_col:
        out["source"] = data[source_col].fillna(source).astype(str)
    else:
        out["source"] = source

    if url_col:
        out["url"] = data[url_col].fillna("").astype(str)
    else:
        out["url"] = ""

    if date_col:
        date_raw = data[date_col].astype(str).str.slice(0, 10)
    elif time_col:
        date_raw = data[time_col].astype(str).str.slice(0, 10)
    else:
        date_raw = datetime.today().strftime("%Y-%m-%d")

    out["date"] = pd.to_datetime(date_raw, errors="coerce")
    out = out.dropna(subset=["date"]).copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")

    if time_col:
        publish_time = pd.to_datetime(data.loc[out.index, time_col], errors="coerce")
        out["publish_time"] = publish_time.fillna(pd.to_datetime(out["date"])).dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    else:
        out["publish_time"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d %H:%M:%S")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out["raw_text_hash"] = [
        stable_hash(row.date, row.title, row.content, row.source, row.url)
        for row in out.itertuples(index=False)
    ]
    out["news_id"] = out["raw_text_hash"].map(lambda x: f"news_{x[:16]}")
    out["created_at"] = now

    out = out.drop_duplicates(subset=["raw_text_hash"], keep="last")
    return out[NEWS_COLUMNS].reset_index(drop=True)


def save_news_items(
    news_df: pd.DataFrame,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> dict:
    init_db(db_path)
    data = normalize_news_dataframe(news_df)

    if data.empty:
        return {"db_path": str(db_path), "input_rows": 0, "inserted_or_updated": 0}

    rows = data.to_dict(orient="records")
    with get_connection(db_path) as conn:
        execute_many(conn, 
            """
            INSERT INTO news_mapping_news_items
                (news_id, date, publish_time, title, content, source, url, raw_text_hash, created_at)
            VALUES
                (%(news_id)s, %(date)s, %(publish_time)s, %(title)s, %(content)s, %(source)s, %(url)s, %(raw_text_hash)s, %(created_at)s)
            ON CONFLICT(news_id) DO UPDATE SET
                date=excluded.date,
                publish_time=excluded.publish_time,
                title=excluded.title,
                content=excluded.content,
                source=excluded.source,
                url=excluded.url
            """,
            rows,
        )
        conn.commit()

    return {
        "db_path": str(db_path),
        "input_rows": int(len(news_df)),
        "normalized_rows": int(len(data)),
        "inserted_or_updated": int(len(rows)),
    }


def ingest_from_csv(path: str | Path, db_path: str | Path = NEWS_MAPPING_DB_PATH) -> dict:
    raw = pd.read_csv(path, encoding="utf-8-sig")
    data = normalize_news_dataframe(raw, source=Path(path).stem)
    return save_news_items(data, db_path=db_path)


def ingest_from_existing_cache(db_path: str | Path = NEWS_MAPPING_DB_PATH) -> dict:
    stock_pool = get_stock_pool(token=None, enrich_name=False)
    events = load_event_cache(stock_pool=stock_pool)
    data = normalize_news_dataframe(events, source="project_event_cache")
    return save_news_items(data, db_path=db_path)


def ingest_from_tushare(
    token: str,
    start_date: str,
    end_date: str,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> dict:
    stock_pool = get_stock_pool(token=token, enrich_name=True)
    events, status = refresh_news_event_cache(
        token=token,
        stock_pool=stock_pool,
        start_date=start_date,
        end_date=end_date,
    )
    data = normalize_news_dataframe(events, source="tushare_event_cache")
    saved = save_news_items(data, db_path=db_path)
    saved["fetch_status"] = status
    return saved


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=str, default="")
    parser.add_argument("--from-cache", action="store_true")
    parser.add_argument("--from-tushare", action="store_true")
    parser.add_argument("--token", type=str, default="")
    parser.add_argument("--use-local-config-token", action="store_true")
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--db-path", type=str, default=str(NEWS_MAPPING_DB_PATH))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    reports = []

    if args.csv:
        reports.append(ingest_from_csv(args.csv, db_path=args.db_path))

    if args.from_cache:
        reports.append(ingest_from_existing_cache(db_path=args.db_path))

    if args.from_tushare:
        token = args.token.strip() or os.getenv("TUSHARE_TOKEN", "").strip()
        if not token and args.use_local_config_token:
            token = str(load_local_config().get("tushare_token") or "").strip()
        if not token:
            raise RuntimeError("从 Tushare 导入新闻需要提供 Token。")
        if not args.start_date or not args.end_date:
            raise RuntimeError("从 Tushare 导入新闻需要 --start-date 和 --end-date。")
        reports.append(
            ingest_from_tushare(
                token=token,
                start_date=args.start_date,
                end_date=args.end_date,
                db_path=args.db_path,
            )
        )

    if not reports:
        reports.append(ingest_from_existing_cache(db_path=args.db_path))

    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
