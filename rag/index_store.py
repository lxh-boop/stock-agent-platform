from __future__ import annotations

from pathlib import Path
from functools import lru_cache
from uuid import uuid4

from rag.bm25_retriever import BM25Retriever
from rag.dense_retriever import DenseRetriever
from storage_governance.lifecycle import prune_stale_atomic_temp_files


def _atomic_save(save_func, path: str | Path) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # The active BM25/Dense path remains the sole index source of truth. Remove
    # only stale temp siblings left by interrupted atomic saves.
    prune_stale_atomic_temp_files(out_path)
    tmp_path = out_path.with_name(f"{out_path.name}.{uuid4().hex}.tmp")
    try:
        save_func(tmp_path)
        tmp_path.replace(out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return out_path


def save_bm25_index(retriever: BM25Retriever, path: str | Path) -> Path:
    return _atomic_save(retriever.save_index, path)


def load_bm25_index(path: str | Path) -> BM25Retriever:
    return BM25Retriever.load_index(path)


def save_dense_index(retriever: DenseRetriever, path: str | Path) -> Path:
    return _atomic_save(retriever.save_index, path)


def load_dense_index(path: str | Path) -> DenseRetriever:
    return DenseRetriever.load_index(path)


@lru_cache(maxsize=4)
def _load_hybrid_index_cached(
    bm25_path: str,
    bm25_mtime_ns: int,
    dense_path: str,
    dense_mtime_ns: int,
):
    # mtimes are part of the cache key so an atomic index replacement is
    # picked up without rebuilding embeddings in the request path.
    del bm25_mtime_ns, dense_mtime_ns
    from rag.hybrid_retriever import HybridRetriever
    from rag.reranker import Reranker

    return HybridRetriever(
        bm25=load_bm25_index(bm25_path),
        dense=load_dense_index(dense_path),
        reranker=Reranker(),
    )


def load_hybrid_index(index_dir: str | Path):
    root = Path(index_dir)
    bm25_path = root / "news_bm25.pkl"
    dense_path = root / "news_dense.pkl"
    if not bm25_path.exists() or not dense_path.exists():
        missing = [str(path.name) for path in (bm25_path, dense_path) if not path.exists()]
        raise FileNotFoundError(f"missing RAG index file(s): {', '.join(missing)}")
    return _load_hybrid_index_cached(
        str(bm25_path.resolve()),
        bm25_path.stat().st_mtime_ns,
        str(dense_path.resolve()),
        dense_path.stat().st_mtime_ns,
    )
