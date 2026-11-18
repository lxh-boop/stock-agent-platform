from __future__ import annotations

import pickle
from pathlib import Path
from types import ModuleType

import jieba
import pytest

from rag.bm25_retriever import BM25Retriever
from rag.index_store import load_bm25_index, save_bm25_index
from rag.schemas import RagChunk


FINANCIAL_TERM = "日更回购合约测试"


@pytest.fixture(autouse=True)
def isolated_account(monkeypatch):
    monkeypatch.setattr("core.user_scope.current_owner", lambda: "")


@pytest.fixture
def chunks():
    return [
        RagChunk(
            "chunk-repurchase", "news-repurchase", 0,
            f"000001 星河公司发布回购股份新闻，{FINANCIAL_TERM}改善业绩并提示政策风险。",
            title="星河公司回购股份", source="synthetic-test", publish_time="2026-11-18 10:00:00",
            stock_codes=["000001"], content_level="full_text",
        ),
        RagChunk(
            "chunk-other", "news-other", 0, "000002 远山公司公告日常生产安排。",
            title="日常经营公告", source="synthetic-test", publish_time="2026-11-18 10:01:00",
            stock_codes=["000002"], content_level="full_text",
        ),
    ]


def search_snapshot(retriever):
    return [result.to_dict() for result in retriever.search(
        "回购 股份 " + FINANCIAL_TERM, top_k=5, metadata_filter={"stock_code": "000001"},
    )]


def test_real_jieba_index_round_trip_keeps_search_and_parameters(tmp_path, chunks):
    original = BM25Retriever(financial_terms=[FINANCIAL_TERM, "回购"], k1=1.8, b=0.6).build_index(chunks)
    assert isinstance(original._jieba, ModuleType)
    expected = search_snapshot(original)
    assert [row["chunk_id"] for row in expected] == ["chunk-repurchase"]

    path = save_bm25_index(original, tmp_path / "news_bm25.pkl")
    assert original._jieba is jieba  # Saving must not disable the live retriever.
    jieba.del_word(FINANCIAL_TERM)
    loaded = load_bm25_index(path)

    assert loaded._jieba is jieba
    assert jieba.get_FREQ(FINANCIAL_TERM)
    assert loaded.financial_terms == original.financial_terms
    assert (loaded.k1, loaded.b, loaded.avgdl) == (original.k1, original.b, original.avgdl)
    assert loaded.tokenized_docs == original.tokenized_docs
    assert loaded.doc_freq == original.doc_freq
    assert loaded.chunks == original.chunks
    assert search_snapshot(loaded) == expected


def test_legacy_fallback_pickle_does_not_switch_tokenizers(tmp_path, chunks, monkeypatch):
    path = tmp_path / "legacy_bm25.pkl"
    with monkeypatch.context() as patch:
        patch.setattr(BM25Retriever, "_load_jieba", lambda _self: None)
        original = BM25Retriever(financial_terms=[FINANCIAL_TERM]).build_index(chunks)
        expected = search_snapshot(original)

        def legacy_state(self):
            state = self.__dict__.copy()
            state.pop("_jieba_enabled", None)
            return state

        # Generate the old object's state layout, without depending on any new
        # serialization implementation or a checked-in binary fixture.
        patch.setattr(BM25Retriever, "__getstate__", legacy_state, raising=False)
        path.write_bytes(pickle.dumps(original))

    loaded = load_bm25_index(path)

    assert loaded._jieba is None
    assert loaded.tokenized_docs == original.tokenized_docs
    assert search_snapshot(loaded) == expected


@pytest.mark.parametrize("jieba_available_on_load", [False, True])
def test_new_fallback_index_keeps_its_backend(tmp_path, chunks, monkeypatch, jieba_available_on_load):
    loader = BM25Retriever._load_jieba
    monkeypatch.setattr(BM25Retriever, "_load_jieba", lambda _self: None)
    original = BM25Retriever(financial_terms=[FINANCIAL_TERM]).build_index(chunks)
    expected = search_snapshot(original)
    path = save_bm25_index(original, tmp_path / "fallback_bm25.pkl")
    if jieba_available_on_load:
        monkeypatch.setattr(BM25Retriever, "_load_jieba", loader)

    loaded = load_bm25_index(path)

    assert loaded._jieba is None
    assert search_snapshot(loaded) == expected


def test_jieba_index_fails_explicitly_if_its_tokenizer_cannot_load(tmp_path, chunks, monkeypatch):
    original = BM25Retriever(financial_terms=[FINANCIAL_TERM]).build_index(chunks)
    assert original._jieba is jieba
    path = save_bm25_index(original, tmp_path / "jieba_bm25.pkl")
    monkeypatch.setattr(BM25Retriever, "_load_jieba", lambda _self: None)

    with pytest.raises(RuntimeError, match="(?i)jieba"):
        load_bm25_index(path)


def test_failed_atomic_save_preserves_the_previous_index(tmp_path, chunks, monkeypatch):
    monkeypatch.setattr(BM25Retriever, "_load_jieba", lambda _self: None)
    original = BM25Retriever().build_index(chunks)
    path = save_bm25_index(original, tmp_path / "news_bm25.pkl")
    previous_bytes = path.read_bytes()
    expected = search_snapshot(load_bm25_index(path))
    replacement = BM25Retriever().build_index(chunks[:1])

    def interrupted_save(temporary):
        Path(temporary).write_bytes(b"incomplete serialized data")
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(replacement, "save_index", interrupted_save)
    with pytest.raises(OSError, match="synthetic disk failure"):
        save_bm25_index(replacement, path)

    assert path.read_bytes() == previous_bytes
    assert list(tmp_path.glob("news_bm25.pkl.*.tmp")) == []
    assert search_snapshot(load_bm25_index(path)) == expected


def test_real_news_index_rebuild_and_persisted_retrieval_smoke_are_offline(tmp_path, chunks, monkeypatch):
    from evaluation import news_rag_diagnostics as diagnostics
    from rag.dense_retriever import DenseRetriever
    from rag.index_store import _load_hybrid_index_cached
    from rag.reranker import Reranker
    from scripts.refresh_news_rag_fulltext import _retrieval_smoke

    def forbid_database(*_args, **_kwargs):
        pytest.fail("The offline index rebuild must not open a database.")

    monkeypatch.setattr(diagnostics, "_chunks_from_db", lambda _path: chunks)
    monkeypatch.setattr(diagnostics, "get_connection", forbid_database)
    # Keep the real Dense serialization and hybrid retrieval paths; only model
    # loading is disabled so no weights, database, or network are required.
    monkeypatch.setattr(DenseRetriever, "_load_model", lambda _self: None)
    monkeypatch.setattr(Reranker, "_load_model", lambda _self: None)
    _load_hybrid_index_cached.cache_clear()
    output = tmp_path / "outputs"
    try:
        report = diagnostics.rebuild_news_rag_indexes(None, output_dir=output)
        smoke = _retrieval_smoke(output, stock_code="000001")
    finally:
        _load_hybrid_index_cached.cache_clear()

    assert report["chunk_count"] == len(chunks)
    assert report["dense_available"] is False
    assert report["embedding_rows_written"] == 0
    assert Path(report["bm25_index_path"]).is_file()
    assert Path(report["dense_index_path"]).is_file()
    assert smoke["success"] is True
    assert smoke["chunk_ids"] == ["chunk-repurchase"]
