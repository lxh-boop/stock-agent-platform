from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from database.repositories import NewsRepository
from evaluation.ragas_eval.schemas import (
    EvaluationCase,
    RetrievedContext,
    decision_time_for_project_filter,
    ensure_list,
    normalize_stock_code,
    parse_optional_datetime,
)
from rag.hybrid_retriever import HybridRetriever
from rag.schemas import RagChunk, RetrievalResult


class ProjectRagAdapter:
    """Adapter around the real project HybridRetriever.

    It converts project RetrievalResult objects into stable offline evaluation
    records. It does not alter the retriever implementation or production
    metadata filtering.
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        chunks: list[RagChunk | dict[str, Any]] | None = None,
        retriever: Any | None = None,
    ) -> None:
        self.db_path = None
        self.warnings: list[str] = []
        if retriever is None and chunks is None:
            try:
                from rag.index_store import load_hybrid_index

                self.retriever = load_hybrid_index(Path("outputs") / "rag_indexes")
                self.chunks = list(self.retriever.bm25.chunks)
                return
            except Exception as exc:
                self.warnings.append(f"persisted hybrid index unavailable; rebuilt from database: {type(exc).__name__}: {exc}")
        if chunks is None:
            chunks = self._load_chunks()
        self.chunks = [chunk if isinstance(chunk, RagChunk) else RagChunk.from_mapping(chunk) for chunk in chunks]
        self.retriever = retriever or HybridRetriever()
        if hasattr(self.retriever, "build_index"):
            self.retriever.build_index(self.chunks)

    def _load_chunks(self) -> list[RagChunk]:
        try:
            rows = NewsRepository().list_news_chunks()
            return [RagChunk.from_mapping(row) for row in rows]
        except Exception as exc:
            self.warnings.append(f"failed to load news chunks from {self.db_path}: {type(exc).__name__}: {exc}")
            return []

    def retrieve(self, case: EvaluationCase, *, top_k: int = 10) -> tuple[list[RetrievedContext], dict[str, Any]]:
        started = time.perf_counter()
        filters = {
            "stock_code": case.stock_code,
            "decision_time": decision_time_for_project_filter(case.decision_time),
            "trade_date_end": case.decision_time.strftime("%Y-%m-%d"),
        }
        raw_results = self.retriever.search(
            case.user_input,
            final_top_k=int(top_k),
            metadata_filter=filters,
        )
        contexts = self._convert_results(raw_results)
        metadata = {
            "adapter": "ProjectRagAdapter",
            "retriever": type(self.retriever).__name__,
            "db_path": str(self.db_path),
            "top_k": int(top_k),
            "filters": filters,
            "chunk_count": len(self.chunks),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "warnings": list(self.warnings),
        }
        return contexts, metadata

    def _convert_results(self, results: list[RetrievalResult | dict[str, Any]]) -> list[RetrievedContext]:
        contexts: list[RetrievedContext] = []
        seen: set[str] = set()
        for index, item in enumerate(results or [], start=1):
            result = item if isinstance(item, RetrievalResult) else RetrievalResult(
                chunk_id=str(item.get("chunk_id") or ""),
                news_id=str(item.get("news_id") or ""),
                chunk_text=str(item.get("chunk_text") or item.get("text") or ""),
                bm25_score=float(item.get("bm25_score", 0.0) or 0.0),
                dense_score=float(item.get("dense_score", 0.0) or 0.0),
                hybrid_score=float(item.get("hybrid_score", 0.0) or 0.0),
                rerank_score=float(item.get("rerank_score", 0.0) or 0.0),
                final_rank=item.get("final_rank"),
                metadata=dict(item.get("metadata") or {}),
            )
            if not result.chunk_id:
                self.warnings.append(f"rank {index} result missing chunk_id; skipped")
                continue
            if result.chunk_id in seen:
                self.warnings.append(f"duplicate retrieved chunk_id={result.chunk_id}; kept first occurrence")
                continue
            seen.add(result.chunk_id)
            contexts.append(self._convert_one(result, rank=result.final_rank or len(contexts) + 1))
        contexts.sort(key=lambda item: item.rank)
        return contexts

    def _convert_one(self, result: RetrievalResult, *, rank: int) -> RetrievedContext:
        meta = dict(result.metadata or {})
        stock_codes = [
            normalize_stock_code(item)
            for item in ensure_list(meta.get("stock_codes") or meta.get("stock_code"))
            if normalize_stock_code(item)
        ]
        warnings: list[str] = []
        if not meta.get("document_id"):
            warnings.append("document_id missing; news_id used as document_id")
        if not meta.get("event_id"):
            warnings.append("event_id missing; duplicate metric will fall back to document_id/title")
        if not meta.get("title"):
            warnings.append("title missing; section_title used when available")
        sources = []
        if result.bm25_score > 0:
            sources.append("bm25")
        if result.dense_score > 0:
            sources.append("dense")
        if result.hybrid_score > 0:
            sources.append("hybrid")
        if result.final_rank is not None or result.rerank_score > 0:
            sources.append("reranker")
        return RetrievedContext(
            chunk_id=result.chunk_id,
            document_id=str(meta.get("document_id") or result.news_id or meta.get("news_id") or "") or None,
            parent_id=str(meta.get("parent_id") or "") or None,
            event_id=str(meta.get("event_id") or "") or None,
            text=result.chunk_text,
            title=str(meta.get("title") or meta.get("section_title") or "") or None,
            stock_codes=stock_codes,
            publish_time=parse_optional_datetime(meta.get("publish_time")),
            source=str(meta.get("source") or "") or None,
            rank=int(rank),
            raw_score=float(result.rerank_score or result.hybrid_score or result.bm25_score or result.dense_score or 0.0),
            retrieval_sources=list(dict.fromkeys(sources)),
            metadata=meta,
            warnings=warnings,
        )
