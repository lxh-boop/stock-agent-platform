from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database.connection import get_connection
from evaluation.news_rag_diagnostics import rebuild_news_rag_indexes
from news_fulltext_ingestion import run_full_text_news_ingestion
from rag.index_store import load_hybrid_index
from universe import get_stock_pool


REQUIRED_CHUNK_COLUMNS = {
    "chunk_id",
    "news_id",
    "chunk_text",
    "title",
    "source",
    "publish_time",
    "stock_code",
    "stock_codes_json",
    "entities_json",
    "industry",
    "event_type",
    "metadata_json",
}


def _json_default(value: Any) -> str:
    return str(value)


def _verify_db() -> dict[str, Any]:
    """Validate the PostgreSQL news/RAG persistence contract."""
    with get_connection() as conn:
        columns = {
            str(row["column_name"])
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema=current_schema()
                   AND table_name='news_chunk'
                """
            ).fetchall()
        }
        missing_columns = sorted(REQUIRED_CHUNK_COLUMNS - columns)

        def scalar(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int((row or {}).get("value") or 0)

        event_count = scalar("SELECT count(*) AS value FROM news_event")
        chunk_count = scalar("SELECT count(*) AS value FROM news_chunk")
        title_only_events = scalar(
            "SELECT count(*) AS value FROM news_event "
            "WHERE COALESCE(content_level,'title_only')!='full_text'"
        )
        title_only_chunks = scalar(
            "SELECT count(*) AS value FROM news_chunk "
            "WHERE COALESCE(content_level,'title_only')!='full_text'"
        )
        metadata_missing = scalar(
            """
            SELECT count(*) AS value FROM news_chunk
             WHERE content_level='full_text'
               AND (
                    TRIM(COALESCE(title,''))=''
                 OR TRIM(COALESCE(source,''))=''
                 OR TRIM(COALESCE(publish_time,''))=''
                 OR TRIM(COALESCE(stock_codes_json,''))=''
                 OR TRIM(COALESCE(entities_json,''))=''
                 OR TRIM(COALESCE(metadata_json,''))=''
               )
            """
        )
        row = conn.execute(
            "SELECT COALESCE(MAX(publish_time),'') AS value "
            "FROM news_event WHERE content_level='full_text'"
        ).fetchone()
        latest = str((row or {}).get("value") or "")

    return {
        "event_count": event_count,
        "chunk_count": chunk_count,
        "title_only_event_count": title_only_events,
        "title_only_chunk_count": title_only_chunks,
        "structured_metadata_missing_count": metadata_missing,
        "latest_publish_time": latest,
        "missing_chunk_columns": missing_columns,
    }


def _retrieval_smoke(output_dir: str | Path, stock_code: str = "") -> dict[str, Any]:
    retriever = load_hybrid_index(Path(output_dir) / "rag_indexes")
    query = f"{stock_code} 新闻 风险 业绩 政策".strip()
    filters = {"stock_code": stock_code} if stock_code else None
    results = retriever.search(query, final_top_k=3, metadata_filter=filters)
    return {
        "success": bool(results),
        "result_count": len(results),
        "chunk_ids": [item.chunk_id for item in results],
        "metadata_sample": [
            {
                "chunk_id": item.chunk_id,
                "news_id": item.news_id,
                "title": (item.metadata or {}).get("title", ""),
                "source": (item.metadata or {}).get("source", ""),
                "publish_time": (item.metadata or {}).get("publish_time", ""),
                "stock_codes": (item.metadata or {}).get("stock_codes", []),
                "entities": (item.metadata or {}).get("entities", []),
                "industry": (item.metadata or {}).get("industry", ""),
                "event_type": (item.metadata or {}).get("event_type", ""),
            }
            for item in results[:3]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Full-text-first public news ingestion, title-only cleanup, structured chunks, and one RAG rebuild."
    )
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--token", default="")
    parser.add_argument("--token-env", default="TUSHARE_TOKEN")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--min-chars", type=int, default=80)
    parser.add_argument("--no-purge-title-only", action="store_true")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--smoke-stock-code", default="")
    args = parser.parse_args(argv)

    token = str(args.token or os.getenv(args.token_env, "") or "").strip()
    print("[News/RAG][1/5] Resolve stock pool...", flush=True)
    stock_pool = get_stock_pool(token=token or None, enrich_name=True)
    print(f"[News/RAG] stock_pool={len(stock_pool)} start={args.start_date} end={args.end_date}", flush=True)

    print("[News/RAG][2/5] Fetch listings and acquire validated full text...", flush=True)
    ingestion = run_full_text_news_ingestion(
        token=token or None,
        stock_pool=stock_pool,
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=args.output_dir,
        workers=max(1, int(args.workers)),
        timeout=max(1.0, float(args.timeout)),
        retries=max(0, int(args.retries)),
        min_chars=max(40, int(args.min_chars)),
        purge_title_only=not args.no_purge_title_only,
    )

    print(
        "[News/RAG] ingestion summary: "
        f"listing={ingestion.listing_rows} full_text_written={ingestion.full_text_articles_written} "
        f"ordinary_status={ingestion.ordinary_news_status} ordinary_listing={ingestion.ordinary_news_listing_rows} "
        f"ordinary_full_text={ingestion.ordinary_news_full_text_written}",
        flush=True,
    )
    print("[News/RAG][3/5] Rebuild persisted BM25/Dense indexes once...", flush=True)
    index_report = rebuild_news_rag_indexes(None, output_dir=args.output_dir)
    print("[News/RAG][4/5] Validate full-text DB contract...", flush=True)
    db_check = _verify_db()
    print("[News/RAG][5/5] Run persisted retrieval smoke...", flush=True)
    smoke = _retrieval_smoke(args.output_dir, stock_code=args.smoke_stock_code)

    failures: list[str] = []
    if db_check["missing_chunk_columns"]:
        failures.append(f"missing_chunk_columns:{db_check['missing_chunk_columns']}")
    if db_check["title_only_event_count"] != 0:
        failures.append(f"title_only_event_count:{db_check['title_only_event_count']}")
    if db_check["title_only_chunk_count"] != 0:
        failures.append(f"title_only_chunk_count:{db_check['title_only_chunk_count']}")
    if db_check["structured_metadata_missing_count"] != 0:
        failures.append(f"structured_metadata_missing_count:{db_check['structured_metadata_missing_count']}")
    if not bool(index_report.get("dense_available")):
        failures.append("dense_index_unavailable")
    if int(index_report.get("chunk_count") or 0) != int(db_check["chunk_count"]):
        failures.append("bm25_chunk_count_mismatch")
    if not smoke["success"]:
        failures.append("persisted_retrieval_smoke_failed")

    payload = {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "ingestion": ingestion.to_dict(),
        "index_report": index_report,
        "db_check": db_check,
        "retrieval_smoke": smoke,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    print(
        f"[News/RAG] DONE status={payload['status']} ordinary_news_status={ingestion.ordinary_news_status} "
        f"ordinary_full_text_written={ingestion.ordinary_news_full_text_written}",
        flush=True,
    )
    if args.report_path:
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(text, encoding="utf-8")
    print(text)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
