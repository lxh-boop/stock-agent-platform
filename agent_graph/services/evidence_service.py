from __future__ import annotations

from core.user_scope import owned_path

from pathlib import Path
from typing import Any
from collections import OrderedDict
from copy import deepcopy
import threading
import time

from database.repositories import NewsRepository as DbNewsRepository

from agent_graph.domain_tools._common import first_present, latest_trade_date, normalize_stock_code


class SourceFormatter:
    def format(self, *, source_type: str, record: dict[str, Any], index: int = 0) -> dict[str, Any]:
        source = {
            "source_type": source_type,
            "source_id": str(
                record.get("chunk_id")
                or record.get("news_id")
                or record.get("source_id")
                or record.get("tool_name")
                or f"{source_type}_{index}"
            ),
            "title": str(record.get("title") or record.get("headline") or record.get("tool_name") or ""),
            "source": str(record.get("source") or record.get("provider_type") or record.get("server_id") or source_type),
            "url": str(record.get("url") or ""),
            "as_of_date": str(first_present(record, ["trade_date", "date", "publish_time"], ""))[:10],
            "provider_type": str(record.get("provider_type") or source_type),
            "untrusted_external_data": bool(
                record.get("untrusted_evidence")
                or str(record.get("provider_type") or "") == "mcp"
            ),
        }
        return {key: value for key, value in source.items() if value not in ("", None)}


class NewsRepository:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = db_path

    def mapped_events(self, stock_code: str, *, as_of_date: str | None = None, limit: int = 10) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        repo = DbNewsRepository(self.db_path)
        mappings = repo.list_news_stock_mappings(stock_code=stock_code)
        events: list[dict[str, Any]] = []
        cutoff = str(as_of_date or "").strip()
        for mapping in mappings:
            news_id = str(mapping.get("news_id") or "")
            event = repo.get_news_event(news_id) if news_id else None
            if not event:
                continue
            event_time = str(first_present(event, ["publish_time", "trade_date"], ""))
            if cutoff and event_time and event_time[:10] > cutoff[:10]:
                continue
            events.append({**event, "mapping": mapping})
        events = sorted(events, key=lambda item: str(item.get("publish_time") or ""), reverse=True)[: max(0, int(limit))]
        return events, mappings[: max(0, int(limit))]


class RagRepository:
    """Process-local cached RAG retrieval with visible stage timings.

    Model downloads are disabled by the retriever classes. If dense/reranker
    models are not already cached locally, the existing BM25/hybrid fallback is
    used immediately instead of blocking an Agent request on network downloads.
    """

    _cache_lock = threading.RLock()
    _cache: "OrderedDict[tuple[Any, ...], list[dict[str, Any]]]" = OrderedDict()
    _cache_max_size = 128

    def __init__(self) -> None:
        self.last_diagnostics: dict[str, Any] = {}

    @staticmethod
    def _index_signature(index_dir: Path) -> tuple[str, int, int]:
        bm25 = index_dir / "news_bm25.pkl"
        dense = index_dir / "news_dense.pkl"
        return (
            str(index_dir.resolve()),
            bm25.stat().st_mtime_ns if bm25.exists() else -1,
            dense.stat().st_mtime_ns if dense.exists() else -1,
        )

    @classmethod
    def _cache_get(cls, key: tuple[Any, ...]) -> list[dict[str, Any]] | None:
        with cls._cache_lock:
            value = cls._cache.get(key)
            if value is None:
                return None
            cls._cache.move_to_end(key)
            return deepcopy(value)

    @classmethod
    def _cache_put(cls, key: tuple[Any, ...], value: list[dict[str, Any]]) -> None:
        with cls._cache_lock:
            cls._cache[key] = deepcopy(value)
            cls._cache.move_to_end(key)
            while len(cls._cache) > cls._cache_max_size:
                cls._cache.popitem(last=False)

    def retrieve_stock_context(
        self,
        *,
        stock_code: str,
        query: str,
        top_k: int = 5,
        output_dir: str | Path = "outputs",
    ) -> list[dict[str, Any]]:
        index_dir = owned_path(output_dir) / "rag_indexes"
        signature = self._index_signature(index_dir)
        final_rerank_limit = 5
        cache_key = (*signature, str(stock_code), str(query), final_rerank_limit)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self.last_diagnostics = {
                "cache_hit": True,
                "index_load_ms": 0.0,
                "search_ms": 0.0,
                "total_ms": 0.0,
                "record_count": len(cached),
            }
            return cached

        total_started = time.perf_counter()
        try:
            from rag.index_store import load_hybrid_index

            load_started = time.perf_counter()
            retriever = load_hybrid_index(index_dir)
            index_load_ms = (time.perf_counter() - load_started) * 1000.0
            search_started = time.perf_counter()
            results = retriever.search(
                query or stock_code,
                final_top_k=final_rerank_limit,
                metadata_filter={"stock_code": stock_code} if stock_code else None,
            )
            search_ms = (time.perf_counter() - search_started) * 1000.0
            records: list[dict[str, Any]] = []
            for item in results:
                metadata = dict(item.metadata or {})
                records.append(
                    {
                        **metadata,
                        "chunk_id": item.chunk_id,
                        "news_id": item.news_id,
                        "text": item.chunk_text,
                        "content": item.chunk_text,
                        "score": float(
                            item.rerank_score
                            or item.hybrid_score
                            or item.bm25_score
                            or item.dense_score
                            or 0.0
                        ),
                        "bm25_score": float(item.bm25_score or 0.0),
                        "dense_score": float(item.dense_score or 0.0),
                        "hybrid_score": float(item.hybrid_score or 0.0),
                        "rerank_score": float(item.rerank_score or 0.0),
                        "retrieval_backend": "bm25_dense_rrf_reranker",
                    }
                )
            total_ms = (time.perf_counter() - total_started) * 1000.0
            self.last_diagnostics = {
                "cache_hit": False,
                "index_load_ms": round(index_load_ms, 3),
                "search_ms": round(search_ms, 3),
                "total_ms": round(total_ms, 3),
                "record_count": len(records),
                "dense_available": bool(getattr(getattr(retriever, "dense", None), "available", False)),
                "reranker_available": bool(getattr(getattr(retriever, "reranker", None), "available", False)),
                "retrieval_policy": dict(getattr(retriever, "last_diagnostics", {}) or {}),
                "final_rerank_limit": final_rerank_limit,
            }
            self._cache_put(cache_key, records)
            return records
        except Exception as exc:
            self.last_diagnostics = {
                "cache_hit": False,
                "fallback_used": False,
                "total_ms": round((time.perf_counter() - total_started) * 1000.0, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc


class EvidenceService:
    def __init__(
        self,
        *,
        source_formatter: SourceFormatter | None = None,
        rag_repository: RagRepository | None = None,
    ) -> None:
        self.source_formatter = source_formatter or SourceFormatter()
        self.rag_repository = rag_repository or RagRepository()

    def _source(self, source_type: str, record: dict[str, Any], index: int = 0) -> dict[str, Any]:
        return self.source_formatter.format(source_type=source_type, record=record, index=index)

    def format_sources(self, records: list[dict[str, Any]], *, source_type: str) -> list[dict[str, Any]]:
        return [self._source(source_type, record, index) for index, record in enumerate(records)]

    def deduplicate_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for source in sources:
            key = (
                str(source.get("source_type") or ""),
                str(source.get("source_id") or ""),
                str(source.get("url") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(source)
        return deduped

    def rank_evidence(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def score(row: dict[str, Any]) -> tuple[float, str]:
            raw_score = row.get("score", row.get("mapping_confidence", 0.0))
            try:
                numeric_score = float(raw_score or 0.0)
            except (TypeError, ValueError):
                numeric_score = 0.0
            date = str(first_present(row, ["publish_time", "trade_date", "date"], ""))
            return numeric_score, date

        return sorted(records, key=score, reverse=True)

    def build_evidence_summary(
        self,
        *,
        records: list[dict[str, Any]],
        stock_code: str = "",
        query: str = "",
        evidence_type: str = "evidence",
    ) -> dict[str, Any]:
        return {
            "evidence_type": evidence_type,
            "stock_code": stock_code,
            "query": query,
            "evidence_count": len(records),
            "record_count": len(records),
            "has_evidence": bool(records),
        }

    def _as_of_date(self, records: list[dict[str, Any]]) -> str:
        return latest_trade_date(records) if records else ""

    def _result(
        self,
        *,
        success: bool,
        message: str,
        query: str = "",
        stock_code: str = "",
        records: list[dict[str, Any]] | None = None,
        sources: list[dict[str, Any]] | None = None,
        summary: dict[str, Any] | None = None,
        status: str = "",
        warnings: list[str] | None = None,
        errors: list[str] | None = None,
        tool_name: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        records = list(records or [])
        sources = self.deduplicate_sources(list(sources or []))
        payload = {
            "query": query,
            "stock_code": stock_code,
            "records": records,
            "summary": summary or self.build_evidence_summary(records=records, stock_code=stock_code, query=query),
            "sources": sources,
            "evidence_count": len(records),
            "as_of_date": self._as_of_date(records),
            "not_executed": True,
            "mutation_performed": False,
        }
        payload.update(dict(extra or {}))
        return {
            "success": bool(success),
            "status": status or ("success" if success else "empty"),
            "message": message,
            "data": payload,
            "records": records,
            "sources": sources,
            "evidence_count": len(records),
            "stock_code": stock_code,
            "query": query,
            "warnings": list(warnings or []),
            "errors": list(errors or []),
            "tool_name": tool_name,
        }

    def search_news(
        self,
        stock_code: str,
        *,
        as_of_date: str | None = None,
        db_path: str | Path | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        code = normalize_stock_code(stock_code)
        if not code:
            return self._result(
                success=False,
                status="invalid_stock_code",
                message="Missing or invalid stock code.",
                stock_code="",
                records=[],
                sources=[],
                errors=["invalid_stock_code"],
                tool_name="evidence.search_news",
                extra={"events": [], "mappings": [], "event_count": 0},
            )
        try:
            events, mappings = NewsRepository(db_path).mapped_events(code, as_of_date=as_of_date, limit=limit)
            sources = self.format_sources(events, source_type="news_event")
            status = "success" if events else "no_news"
            return self._result(
                success=bool(events),
                status=status,
                message="News evidence queried." if events else "No mapped news evidence was found.",
                stock_code=code,
                records=events,
                sources=sources,
                summary=self.build_evidence_summary(records=events, stock_code=code, evidence_type="news"),
                tool_name="evidence.search_news",
                extra={"events": events, "mappings": mappings, "event_count": len(events)},
            )
        except Exception as exc:
            return self._result(
                success=False,
                status="error",
                message="News evidence query failed.",
                stock_code=code,
                records=[],
                errors=[f"{type(exc).__name__}: {exc}"],
                tool_name="evidence.search_news",
                extra={"events": [], "mappings": [], "event_count": 0, "error": f"{type(exc).__name__}: {exc}"},
            )

    def search_rag(
        self,
        stock_code: str,
        *,
        query: str = "",
        top_k: int = 5,
        output_dir: str | Path = "outputs",
    ) -> dict[str, Any]:
        code = normalize_stock_code(stock_code)
        if not code:
            return self._result(
                success=False,
                status="invalid_stock_code",
                message="Missing or invalid stock code.",
                stock_code="",
                query=query,
                records=[],
                errors=["invalid_stock_code"],
                tool_name="evidence.search_rag",
                extra={"chunks": []},
            )
        try:
            chunks = self.rag_repository.retrieve_stock_context(
                stock_code=code,
                query=query or code,
                top_k=top_k,
                output_dir=output_dir,
            )
            sources = self.format_sources(chunks, source_type="rag_chunk")
            status = "success" if chunks else "no_rag_chunks"
            return self._result(
                success=bool(chunks),
                status=status,
                message=(
                    "RAG evidence queried."
                    if chunks
                    else "No RAG chunks were found."
                ),
                stock_code=code,
                query=query or code,
                records=chunks,
                sources=sources,
                summary=self.build_evidence_summary(
                    records=chunks,
                    stock_code=code,
                    query=query or code,
                    evidence_type="rag",
                ),
                tool_name="evidence.search_rag",
                extra={
                    "chunks": chunks,
                    "retrieval_diagnostics": dict(
                        self.rag_repository.last_diagnostics or {}
                    ),
                },
            )
        except Exception as exc:
            return self._result(
                success=False,
                status="unavailable",
                message="RAG evidence is unavailable; returned an empty evidence set.",
                stock_code=code,
                query=query or code,
                records=[],
                warnings=["rag_unavailable"],
                errors=[f"{type(exc).__name__}: {exc}"],
                tool_name="evidence.search_rag",
                extra={
                    "chunks": [],
                    "error": f"{type(exc).__name__}: {exc}",
                    "retrieval_diagnostics": dict(
                        self.rag_repository.last_diagnostics or {}
                    ),
                },
            )

    def search_documents(
        self,
        query: str,
        *,
        stock_code: str = "",
        top_k: int = 5,
        output_dir: str | Path = "outputs",
    ) -> dict[str, Any]:
        code = normalize_stock_code(stock_code) if stock_code else ""
        query_text = str(query or "").strip()
        if not query_text:
            return self._result(
                success=False,
                status="invalid_query",
                message="A document search query is required.",
                stock_code=code,
                records=[],
                errors=["invalid_query"],
                tool_name="evidence.search_documents",
                extra={"chunks": []},
            )
        try:
            chunks = self.rag_repository.retrieve_stock_context(
                stock_code=code,
                query=query_text,
                top_k=top_k,
                output_dir=output_dir,
            )
            sources = self.format_sources(chunks, source_type="rag_chunk")
            return self._result(
                success=bool(chunks),
                status="success" if chunks else "no_rag_chunks",
                message=(
                    "RAG documents queried."
                    if chunks
                    else "No RAG documents were found."
                ),
                stock_code=code,
                query=query_text,
                records=chunks,
                sources=sources,
                summary=self.build_evidence_summary(
                    records=chunks,
                    stock_code=code,
                    query=query_text,
                    evidence_type="rag",
                ),
                tool_name="evidence.search_documents",
                extra={
                    "chunks": chunks,
                    "retrieval_diagnostics": dict(
                        self.rag_repository.last_diagnostics or {}
                    ),
                },
            )
        except Exception as exc:
            return self._result(
                success=False,
                status="unavailable",
                message="RAG document search is unavailable.",
                stock_code=code,
                query=query_text,
                records=[],
                warnings=["rag_unavailable"],
                errors=[f"{type(exc).__name__}: {exc}"],
                tool_name="evidence.search_documents",
                extra={
                    "chunks": [],
                    "retrieval_diagnostics": dict(
                        self.rag_repository.last_diagnostics or {}
                    ),
                },
            )
    def merge_evidence(self, *payloads: dict[str, Any]) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        warnings: list[str] = []
        errors: list[str] = []
        for payload in payloads:
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            records.extend(data.get("records") or payload.get("records") or [])
            sources.extend(data.get("sources") or payload.get("sources") or [])
            warnings.extend(payload.get("warnings") or [])
            errors.extend(payload.get("errors") or [])
        records = self.rank_evidence(records)
        return {
            "records": records,
            "sources": self.deduplicate_sources(sources),
            "warnings": warnings,
            "errors": errors,
        }

    def get_stock_evidence(
        self,
        stock_code: str,
        *,
        query: str = "",
        as_of_date: str | None = None,
        top_k: int = 5,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        news = self.search_news(stock_code, as_of_date=as_of_date, db_path=db_path, limit=top_k)
        rag = self.search_rag(stock_code, query=query or f"{stock_code} risk evidence", top_k=top_k, output_dir=output_dir)
        merged = self.merge_evidence(news, rag)
        code = normalize_stock_code(stock_code)
        return self._result(
            success=bool(merged["records"]),
            message="Stock evidence collected." if merged["records"] else "No stock evidence was found.",
            stock_code=code,
            query=query,
            records=merged["records"],
            sources=merged["sources"],
            warnings=merged["warnings"],
            errors=merged["errors"],
            summary=self.build_evidence_summary(records=merged["records"], stock_code=code, query=query, evidence_type="stock_evidence"),
            tool_name="evidence.get_stock_evidence",
            extra={"news": news.get("data") or {}, "rag": rag.get("data") or {}},
        )

    def get_market_evidence(
        self,
        *,
        query: str = "",
        stock_codes: list[str] | str | None = None,
        as_of_date: str | None = None,
        top_k: int = 5,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        if isinstance(stock_codes, str):
            codes = [item.strip() for item in stock_codes.replace(";", ",").split(",") if item.strip()]
        else:
            codes = [str(item).strip() for item in (stock_codes or []) if str(item).strip()]
        results = [
            self.get_stock_evidence(
                code,
                query=query,
                as_of_date=as_of_date,
                top_k=top_k,
                output_dir=output_dir,
                db_path=db_path,
            )
            for code in codes[: max(0, int(top_k))]
        ]
        merged = self.merge_evidence(*results)
        return self._result(
            success=bool(merged["records"]),
            message="Market evidence collected." if merged["records"] else "No market evidence was found.",
            query=query,
            records=merged["records"],
            sources=merged["sources"],
            warnings=merged["warnings"],
            errors=merged["errors"],
            summary={
                **self.build_evidence_summary(records=merged["records"], query=query, evidence_type="market_evidence"),
                "stock_codes": [normalize_stock_code(code) for code in codes if normalize_stock_code(code)],
            },
            tool_name="evidence.get_market_evidence",
        )

evidence_service = EvidenceService()
