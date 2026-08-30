from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from database.connection import get_connection
from database.repositories import NewsRepository
from news_db_sync import _chunk_statistics
from rag.bm25_retriever import BM25Retriever
from rag.dense_retriever import DenseRetriever
from rag.hybrid_retriever import HybridRetriever
from rag.index_store import save_bm25_index, save_dense_index
from rag.metadata_filter import metadata_matches
from rag.reranker import Reranker
from rag.schemas import RagChunk


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: str
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _chunks_from_db(db_path: str | Path | None) -> list[RagChunk]:
    repo = NewsRepository(db_path)
    rows = repo.list_news_chunks()
    return [RagChunk.from_mapping(row) for row in rows]


def _events_from_db(db_path: str | Path | None) -> list[dict[str, Any]]:
    return NewsRepository(db_path).list_news_events()


def collect_news_chunk_statistics(db_path: str | Path | None) -> dict[str, Any]:
    events = _events_from_db(db_path)
    chunk_objects = _chunks_from_db(db_path)
    chunks = [chunk.to_database_record() for chunk in chunk_objects]
    stock_by_news: dict[str, str] = {}
    for chunk in chunk_objects:
        if chunk.news_id and chunk.stock_codes:
            stock_by_news.setdefault(chunk.news_id, chunk.stock_codes[0])
    stats_events = [
        {
            "news_id": event.get("news_id", ""),
            "stock_code": stock_by_news.get(str(event.get("news_id") or ""), ""),
            "source": event.get("source", ""),
            "content_level": event.get("content_level") or "title_only",
            "summary": event.get("summary", ""),
            "content": event.get("content", ""),
        }
        for event in events
    ]
    return _chunk_statistics(events=stats_events, chunks=chunks)


def rebuild_news_rag_indexes(
    db_path: str | Path | None,
    output_dir: str | Path = "outputs",
) -> dict[str, Any]:
    chunks = _chunks_from_db(db_path)
    dense_chunks = _dense_index_chunks(chunks)
    index_dir = Path(output_dir) / "rag_indexes"
    bm25 = BM25Retriever().build_index(chunks)
    dense = DenseRetriever().build_index(dense_chunks)
    bm25_path = save_bm25_index(bm25, index_dir / "news_bm25.pkl")
    dense_path = save_dense_index(dense, index_dir / "news_dense.pkl")
    dense_status = dense.status()
    embedding_rows = _persist_dense_embeddings(db_path, dense, dense_path)
    return {
        "chunk_count": len(chunks),
        "dense_chunk_count": len(dense_chunks),
        "bm25_index_path": str(bm25_path),
        "dense_index_path": str(dense_path),
        "dense_available": bool(dense.available),
        "dense_model": dense.model_name,
        "embedding_model_name": dense_status["embedding_model_name"],
        "embedding_dimension": dense_status["embedding_dimension"],
        "embedding_rows_written": embedding_rows,
        "index_version": dense_status["index_version"],
        "dense_status": dense_status,
    }


def _dense_index_chunks(chunks: list[RagChunk]) -> list[RagChunk]:
    full_text_chunks = [chunk for chunk in chunks if str(chunk.content_level or "") == "full_text"]
    return full_text_chunks or chunks


def _persist_dense_embeddings(
    db_path: str | Path | None,
    dense: DenseRetriever,
    dense_path: str | Path,
) -> int:
    embeddings = getattr(dense, "embeddings", None)
    chunks = list(getattr(dense, "chunks", []) or [])
    model_name = str(getattr(dense, "embedding_model_name", "") or getattr(dense, "model_name", "") or "")
    dim = int(getattr(dense, "embedding_dimension", 0) or 0)
    if not getattr(dense, "available", False) or embeddings is None or dim <= 0 or not chunks:
        return 0
    array = np.asarray(embeddings, dtype=np.float32)
    if array.ndim != 2 or array.shape[0] != len(chunks) or array.shape[1] <= 0:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: list[dict[str, Any]] = []
    for chunk, vector in zip(chunks, array):
        content_hash = hashlib.sha1(str(chunk.chunk_text or "").encode("utf-8")).hexdigest()[:16]
        embedding_id = hashlib.sha1(
            f"{chunk.chunk_id}\n{model_name}\n{content_hash}".encode("utf-8")
        ).hexdigest()
        rows.append(
            {
                "embedding_id": f"emb_{embedding_id[:24]}",
                "chunk_id": chunk.chunk_id,
                "embedding_model": model_name,
                "embedding_dim": int(array.shape[1]),
                "embedding_path": str(dense_path),
                "embedding": vector.astype(np.float32).tobytes(),
                "created_at": now,
            }
        )
    with get_connection() as conn:
        # Keep the embedding table aligned with the current official dense
        # index for this model. This removes stale rows from interrupted or
        # previous wider index builds without touching other embedding models.
        conn.execute("DELETE FROM news_embedding WHERE embedding_model = %s", (model_name,))
        conn.executemany(
            """
            INSERT INTO news_embedding (
                embedding_id, chunk_id, embedding_model, embedding_dim,
                embedding_path, embedding, created_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (embedding_id) DO UPDATE SET
                chunk_id = excluded.chunk_id, embedding_model = excluded.embedding_model,
                embedding_dim = excluded.embedding_dim, embedding_path = excluded.embedding_path,
                embedding = excluded.embedding, created_at = excluded.created_at
            """,
            [(r["embedding_id"], r["chunk_id"], r["embedding_model"], r["embedding_dim"], r["embedding_path"], r["embedding"], r["created_at"]) for r in rows],
        )
        conn.commit()
    return len(rows)


def _check_future_leakage(chunks: list[RagChunk], decision_time: str) -> DiagnosticCheck:
    if not decision_time:
        return DiagnosticCheck("future_information_leakage", "skipped", True, {"reason": "decision_time_not_provided"})
    returned = [chunk for chunk in chunks if metadata_matches(chunk, {"decision_time": decision_time})]
    leaked = [
        chunk.chunk_id
        for chunk in returned
        if not metadata_matches(chunk, {"decision_time": decision_time})
    ]
    blocked = len(chunks) - len(returned)
    return DiagnosticCheck(
        "future_information_leakage",
        "passed" if not leaked else "failed",
        not leaked,
        {
            "decision_time": decision_time,
            "returned_chunk_count": len(returned),
            "future_blocked_count": blocked,
            "leaked_chunk_count": len(leaked),
            "sample_chunk_ids": leaked[:10],
        },
    )


def _check_wrong_stock(chunks: list[RagChunk], stock_code: str) -> DiagnosticCheck:
    if not stock_code:
        return DiagnosticCheck("wrong_stock_filter", "skipped", True, {"reason": "stock_code_not_provided"})
    target = str(stock_code).split(".")[0].zfill(6)
    wrong = [
        chunk.chunk_id
        for chunk in chunks
        if metadata_matches(chunk, {"stock_code": target}) and target not in set(chunk.stock_codes)
    ]
    return DiagnosticCheck(
        "wrong_stock_filter",
        "passed" if not wrong else "failed",
        not wrong,
        {"stock_code": target, "wrong_chunk_count": len(wrong), "sample_chunk_ids": wrong[:10]},
    )


def _check_duplicates(chunks: list[RagChunk]) -> DiagnosticCheck:
    rows = [
        {
            "news_id": chunk.news_id,
            "stock_code": chunk.stock_codes[0] if chunk.stock_codes else "",
            "chunk_index": chunk.chunk_index,
            "chunk_text": chunk.chunk_text,
        }
        for chunk in chunks
    ]
    if not rows:
        return DiagnosticCheck("duplicate_chunks", "skipped", True, {"reason": "no_chunks"})
    frame = pd.DataFrame(rows)
    duplicate_keys = frame.duplicated(subset=["news_id", "chunk_index"], keep=False)
    duplicate_text = frame.duplicated(subset=["news_id", "stock_code", "chunk_text"], keep=False)
    duplicate_count = int((duplicate_keys | duplicate_text).sum())
    return DiagnosticCheck(
        "duplicate_chunks",
        "passed" if duplicate_count == 0 else "failed",
        duplicate_count == 0,
        {"duplicate_count": duplicate_count},
    )


def _check_content_integrity(events: list[dict[str, Any]], chunks: list[RagChunk]) -> list[DiagnosticCheck]:
    empty_chunks = [chunk.chunk_id for chunk in chunks if not chunk.chunk_text.strip()]
    empty_full_text = [
        str(event.get("news_id") or "")
        for event in events
        if str(event.get("content_level") or "") == "full_text" and not str(event.get("content") or "").strip()
    ]
    title_as_full = [
        str(event.get("news_id") or "")
        for event in events
        if str(event.get("content_level") or "") == "full_text"
        and str(event.get("content") or "").strip()
        and str(event.get("content") or "").strip() == str(event.get("title") or "").strip()
    ]
    return [
        DiagnosticCheck(
            "empty_chunk_text",
            "passed" if not empty_chunks else "failed",
            not empty_chunks,
            {"empty_chunk_count": len(empty_chunks), "sample_chunk_ids": empty_chunks[:10]},
        ),
        DiagnosticCheck(
            "empty_full_text_body",
            "passed" if not empty_full_text else "failed",
            not empty_full_text,
            {"event_count": len(empty_full_text), "sample_news_ids": empty_full_text[:10]},
        ),
        DiagnosticCheck(
            "title_as_full_text",
            "passed" if not title_as_full else "failed",
            not title_as_full,
            {"event_count": len(title_as_full), "sample_news_ids": title_as_full[:10]},
        ),
    ]


def _check_traceability(chunks: list[RagChunk]) -> DiagnosticCheck:
    missing = [
        chunk.chunk_id
        for chunk in chunks
        if not chunk.news_id or not chunk.source or not chunk.publish_time or not (chunk.stock_codes and chunk.stock_codes[0])
    ]
    return DiagnosticCheck(
        "retrieval_source_traceability",
        "passed" if not missing else "failed",
        not missing,
        {"missing_trace_count": len(missing), "sample_chunk_ids": missing[:10]},
    )


def _check_retrieval_chain(
    chunks: list[RagChunk],
    *,
    query: str,
    stock_code: str = "",
    decision_time: str = "",
    require_dense: bool = False,
) -> list[DiagnosticCheck]:
    if not chunks:
        return [
            DiagnosticCheck("bm25_exact_hit", "skipped", True, {"reason": "no_chunks"}),
            DiagnosticCheck("dense_semantic_recall", "skipped", True, {"reason": "no_chunks"}),
            DiagnosticCheck("hybrid_reranker_topk", "skipped", True, {"reason": "no_chunks"}),
        ]
    filters: dict[str, Any] = {}
    if stock_code:
        filters["stock_code"] = str(stock_code).split(".")[0].zfill(6)
    if decision_time:
        filters["decision_time"] = decision_time

    dense_chunks = _dense_index_chunks(chunks)
    bm25 = BM25Retriever().build_index(chunks)
    dense = DenseRetriever().build_index(dense_chunks)
    retriever = HybridRetriever(bm25=bm25, dense=dense, reranker=Reranker())

    bm25_results = bm25.search(query, top_k=5, metadata_filter=filters)
    dense_results = dense.search(query, top_k=5, metadata_filter=filters)
    hybrid_results = retriever.search(query, final_top_k=5, metadata_filter=filters)
    bm25_exact = bool(bm25_results)
    dense_ok = bool(dense_results) if dense.available else not require_dense
    dense_status = dense.status()
    rerank_ok = bool(hybrid_results) and all(result.final_rank for result in hybrid_results)
    return [
        DiagnosticCheck(
            "bm25_exact_hit",
            "passed" if bm25_exact else "failed",
            bm25_exact,
            {"returned": len(bm25_results), "chunk_ids": [item.chunk_id for item in bm25_results]},
        ),
        DiagnosticCheck(
            "dense_semantic_recall",
            "passed" if dense_ok else "failed",
            dense_ok,
            {
                "dense_available": bool(dense.available),
                "returned": len(dense_results),
                "chunk_ids": [item.chunk_id for item in dense_results],
                "require_dense": bool(require_dense),
                "dense_status": dense_status,
            },
        ),
        DiagnosticCheck(
            "hybrid_reranker_topk",
            "passed" if rerank_ok else "failed",
            rerank_ok,
            {"returned": len(hybrid_results), "chunk_ids": [item.chunk_id for item in hybrid_results]},
        ),
    ]


def run_news_rag_diagnostics(
    db_path: str | Path | None,
    *,
    query: str = "",
    stock_code: str = "",
    decision_time: str = "",
    output_dir: str | Path = "outputs",
    rebuild_indexes: bool = True,
    require_dense: bool = False,
) -> dict[str, Any]:
    events = _events_from_db(db_path)
    chunks = _chunks_from_db(db_path)
    query_text = query or " ".join(part for part in [stock_code, "新闻 风险 公告"] if part).strip() or "新闻 风险 公告"
    checks: list[DiagnosticCheck] = [
        _check_future_leakage(chunks, decision_time),
        _check_wrong_stock(chunks, stock_code),
        _check_duplicates(chunks),
        _check_traceability(chunks),
    ]
    checks.extend(_check_content_integrity(events, chunks))
    checks.extend(
        _check_retrieval_chain(
            chunks,
            query=query_text,
            stock_code=stock_code,
            decision_time=decision_time,
            require_dense=require_dense,
        )
    )
    stats = collect_news_chunk_statistics(db_path)
    for check in checks:
        if check.name == "future_information_leakage":
            stats["future_news_filtered_count"] = int(check.details.get("future_blocked_count") or 0)
            break
    index_report = rebuild_news_rag_indexes(db_path, output_dir=output_dir) if rebuild_indexes else {}
    passed = sum(1 for check in checks if check.passed)
    failed = sum(1 for check in checks if not check.passed)
    return {
        "acceptance_eligible": False,
        "diagnostic_only": True,
        "db_path": str(db_path or ""),
        "query": query_text,
        "stock_code": str(stock_code or ""),
        "decision_time": str(decision_time or ""),
        "statistics": stats,
        "index_report": index_report,
        "require_dense": bool(require_dense),
        "checks": [check.to_dict() for check in checks],
        "summary": {
            "check_count": len(checks),
            "passed": passed,
            "failed": failed,
            "pass_rate": passed / len(checks) if checks else 1.0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run diagnostic-only news RAG checks.")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--output-dir", default="outputs")
    parser.add_argument("--query", default="")
    parser.add_argument("--stock-code", default="")
    parser.add_argument("--decision-time", default="")
    parser.add_argument("--no-rebuild-indexes", action="store_true")
    parser.add_argument("--require-dense", action="store_true")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args(argv)
    report = run_news_rag_diagnostics(
        args.db_path,
        query=args.query,
        stock_code=args.stock_code,
        decision_time=args.decision_time,
        output_dir=args.output_dir,
        rebuild_indexes=not args.no_rebuild_indexes,
        require_dense=args.require_dense,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.report_path:
        path = Path(args.report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
