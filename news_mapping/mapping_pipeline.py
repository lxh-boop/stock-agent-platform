from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from config import OUTPUT_DIR
from local_config import load_local_config
from .concept_mapper import map_concepts_to_stocks, seed_default_concept_stock_map
from .entity_extractor import extract_entities_for_news_row
from .llm_mapper import map_news_with_llm
from .mapping_store import store_mapping_result
from .news_ingestor import ingest_from_existing_cache, ingest_from_tushare
from .schema import NEWS_MAPPING_DB_PATH, get_connection, init_db
from .stock_alias_builder import build_stock_alias_table


def get_table_count(table: str, db_path: str | Path = NEWS_MAPPING_DB_PATH) -> int:
    with get_connection(db_path) as conn:
        row = conn.execute(f"SELECT COUNT(*) AS row_count FROM {table}").fetchone()
        return int((row or {}).get("row_count") or 0)


def load_news_for_date(
    date: str | None,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
    limit: int | None = None,
) -> pd.DataFrame:
    init_db(db_path)
    with get_connection(db_path) as conn:
        if not date:
            row = conn.execute("SELECT MAX(date) AS max_date FROM news_mapping_news_items").fetchone()
            date = row["max_date"] if row else None
        if not date:
            return pd.DataFrame()

        sql = """
            SELECT news_id, date, publish_time, title, content, source, url
            FROM news_mapping_news_items
            WHERE date = %s
            ORDER BY publish_time DESC, news_id
        """
        params: list = [date]
        if limit and limit > 0:
            sql += " LIMIT %s"
            params.append(int(limit))
        return pd.read_sql_query(sql, conn, params=params)


def export_mapping_for_date(
    date: str,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> pd.DataFrame:
    with get_connection(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT
                n.date,
                n.publish_time,
                n.news_id,
                n.title,
                n.source,
                l.code,
                l.name,
                l.link_type,
                l.confidence,
                l.reason,
                l.evidence,
                l.mapper,
                l.status
            FROM news_mapping_news_items n
            JOIN news_mapping_news_stock_links l
              ON n.news_id = l.news_id
            WHERE n.date = %s
            ORDER BY n.publish_time DESC, l.status, l.confidence DESC
            """,
            conn,
            params=[date],
        )

    out_path = Path(OUTPUT_DIR) / f"news_stock_mapping_{date.replace('-', '')}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return df


def export_simple_event_features(mapping_df: pd.DataFrame, date: str) -> pd.DataFrame:
    output_path = Path(OUTPUT_DIR) / f"news_event_features_{date.replace('-', '')}.csv"
    if mapping_df.empty:
        out = pd.DataFrame(
            columns=[
                "date",
                "code",
                "news_count_1d",
                "direct_news_count_1d",
                "concept_news_count_1d",
                "news_attention_score",
            ]
        )
        out.to_csv(output_path, index=False, encoding="utf-8-sig")
        return out

    valid = mapping_df[mapping_df["status"].isin(["auto_confirmed", "manual_confirmed", "pending_review"])].copy()
    valid["is_direct"] = valid["link_type"].eq("direct_company").astype(int)
    valid["is_concept"] = valid["link_type"].isin(["concept", "industry_chain", "macro_industry"]).astype(int)
    grouped = valid.groupby(["date", "code"], as_index=False).agg(
        news_count_1d=("news_id", "nunique"),
        direct_news_count_1d=("is_direct", "sum"),
        concept_news_count_1d=("is_concept", "sum"),
        news_attention_score=("confidence", "sum"),
    )
    grouped.to_csv(output_path, index=False, encoding="utf-8-sig")
    return grouped


def run_mapping_pipeline(
    date: str | None = None,
    token: str | None = None,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
    with_llm: bool = False,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    limit: int | None = None,
    refresh_tushare: bool = False,
) -> dict:
    init_db(db_path)

    if get_table_count("news_mapping_stock_alias", db_path) == 0:
        build_stock_alias_table(token=token, db_path=db_path)

    if get_table_count("news_mapping_concept_stock_map", db_path) == 0:
        seed_default_concept_stock_map(db_path=db_path)

    if refresh_tushare and token:
        if not date:
            raise RuntimeError("refresh_tushare=True 时需要指定 date。")
        ingest_report = ingest_from_tushare(
            token=token,
            start_date=date,
            end_date=date,
            db_path=db_path,
        )
    else:
        ingest_report = ingest_from_existing_cache(db_path=db_path)

    news_df = load_news_for_date(date=date, db_path=db_path, limit=limit)
    if news_df.empty:
        return {
            "date": date,
            "news_rows": 0,
            "processed_news": 0,
            "saved_links": 0,
            "ingest_report": ingest_report,
        }

    run_date = str(news_df["date"].iloc[0])
    total_saved = 0
    total_skipped = 0
    processed = 0
    llm_calls = 0

    for news_item in news_df.to_dict(orient="records"):
        rule_extract = extract_entities_for_news_row(news_item, db_path=db_path)
        concept_links = map_concepts_to_stocks(
            rule_extract.get("candidate_concepts", []),
            db_path=db_path,
            max_stocks_per_concept=30,
        )
        llm_result = None
        if with_llm and api_key:
            llm_result = map_news_with_llm(
                news_item=news_item,
                api_key=api_key,
                base_url=base_url,
                model=model,
                db_path=db_path,
                rule_extract=rule_extract,
                concept_links=concept_links,
            )
            llm_calls += 1

        store_report = store_mapping_result(
            news_item=news_item,
            rule_extract=rule_extract,
            concept_links=concept_links,
            llm_result=llm_result,
            db_path=db_path,
        )
        total_saved += int(store_report.get("total_saved", 0))
        total_skipped += int(store_report.get("total_skipped", 0))
        processed += 1

    mapping_df = export_mapping_for_date(run_date, db_path=db_path)
    feature_df = export_simple_event_features(mapping_df, run_date)

    status_counts = (
        mapping_df["status"].value_counts().to_dict()
        if not mapping_df.empty and "status" in mapping_df.columns
        else {}
    )

    return {
        "date": run_date,
        "news_rows": int(len(news_df)),
        "processed_news": processed,
        "saved_links_in_run": total_saved,
        "skipped_links_in_run": total_skipped,
        "llm_calls": llm_calls,
        "mapping_rows_for_date": int(len(mapping_df)),
        "event_feature_rows": int(len(feature_df)),
        "status_counts_for_date": status_counts,
        "ingest_report": ingest_report,
        "mapping_output": str(Path(OUTPUT_DIR) / f"news_stock_mapping_{run_date.replace('-', '')}.csv"),
        "feature_output": str(Path(OUTPUT_DIR) / f"news_event_features_{run_date.replace('-', '')}.csv"),
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default="")
    parser.add_argument("--token", type=str, default="")
    parser.add_argument("--use-local-config-token", action="store_true")
    parser.add_argument("--db-path", type=str, default=str(NEWS_MAPPING_DB_PATH))
    parser.add_argument("--with-llm", action="store_true")
    parser.add_argument("--use-local-config-ai", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--refresh-tushare", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_local_config()
    token = args.token or ""
    if not token and args.use_local_config_token:
        token = str(cfg.get("tushare_token") or "")

    api_key = ""
    base_url = ""
    model = ""
    if args.with_llm and args.use_local_config_ai:
        api_key = str(cfg.get("llm_api_key") or "")
        base_url = str(cfg.get("llm_base_url") or "")
        model = str(cfg.get("llm_model") or "")

    report = run_mapping_pipeline(
        date=args.date or None,
        token=token or None,
        db_path=args.db_path,
        with_llm=args.with_llm,
        api_key=api_key,
        base_url=base_url,
        model=model,
        limit=args.limit or None,
        refresh_tushare=args.refresh_tushare,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
