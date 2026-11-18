from __future__ import annotations

import math
import pickle
import re
from collections import Counter
from pathlib import Path
from typing import Any

from rag.metadata_filter import filter_chunks
from rag.schemas import RagChunk, RetrievalResult
from rag.utils import DEFAULT_EVENT_TERMS, clean_text


DEFAULT_BM25_MIN_SIMILARITY = 0.20
DEFAULT_RECALL_CANDIDATE_LIMIT = 200


class BM25Retriever:
    def __init__(self, financial_terms: list[str] | None = None, k1: float = 1.5, b: float = 0.75):
        self.financial_terms = list(dict.fromkeys((financial_terms or []) + DEFAULT_EVENT_TERMS))
        self.k1 = float(k1)
        self.b = float(b)
        self.chunks: list[RagChunk] = []
        self.tokenized_docs: list[list[str]] = []
        self.doc_freq: Counter[str] = Counter()
        self.avgdl = 0.0
        self._jieba = self._load_jieba()

    def __getstate__(self) -> dict[str, Any]:
        state = self.__dict__.copy()
        # Persist the tokenizer choice, not the imported module or its runtime locks.
        state["_jieba_enabled"] = state.pop("_jieba", None) is not None
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        restored = state.copy()
        # Older successful pickles used the regex fallback (_jieba=None).
        # Keep that query tokenizer consistent with their stored document tokens.
        jieba_enabled = bool(restored.pop("_jieba_enabled", False))
        restored.pop("_jieba", None)
        self.__dict__.update(restored)
        self._jieba = self._load_jieba() if jieba_enabled else None
        if jieba_enabled and self._jieba is None:
            raise RuntimeError("BM25 索引需要 jieba 分词器，当前不可用；请恢复该依赖或重新构建索引。")

    def _load_jieba(self):
        try:
            import jieba

            for term in self.financial_terms:
                if term:
                    jieba.add_word(str(term))
            return jieba
        except Exception:
            return None

    def tokenize(self, text: str) -> list[str]:
        text = clean_text(text)
        tokens: list[str] = []
        if self._jieba is not None:
            tokens.extend([token.strip() for token in self._jieba.lcut(text) if token.strip()])
        else:
            tokens.extend(re.findall(r"[A-Za-z0-9_.]+", text))
            chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
            tokens.extend(chinese_chars)
            tokens.extend("".join(chinese_chars[i : i + 2]) for i in range(max(0, len(chinese_chars) - 1)))
            tokens.extend("".join(chinese_chars[i : i + 3]) for i in range(max(0, len(chinese_chars) - 2)))
        for term in self.financial_terms:
            term = str(term).strip()
            if term and term in text:
                tokens.append(term)
        return tokens

    def build_index(self, chunks: list[RagChunk | dict[str, Any]]) -> "BM25Retriever":
        self.chunks = [chunk if isinstance(chunk, RagChunk) else RagChunk.from_mapping(chunk) for chunk in chunks]
        self.tokenized_docs = [self.tokenize(chunk.chunk_text) for chunk in self.chunks]
        self.doc_freq = Counter()
        for tokens in self.tokenized_docs:
            self.doc_freq.update(set(tokens))
        self.avgdl = sum(len(tokens) for tokens in self.tokenized_docs) / max(1, len(self.tokenized_docs))
        return self

    def _idf(self, token: str) -> float:
        n_docs = len(self.tokenized_docs)
        df = self.doc_freq.get(token, 0)
        return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))

    def _score_tokens(self, query_tokens: list[str], doc_tokens: list[str]) -> float:
        if not query_tokens or not doc_tokens:
            return 0.0
        counts = Counter(doc_tokens)
        doc_len = len(doc_tokens)
        score = 0.0
        for token in query_tokens:
            freq = counts.get(token, 0)
            if freq <= 0:
                continue
            numerator = freq * (self.k1 + 1)
            denominator = freq + self.k1 * (1 - self.b + self.b * doc_len / max(self.avgdl, 1e-9))
            score += self._idf(token) * numerator / denominator
        return float(score)

    def search(
        self,
        query: str,
        top_k: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
        *,
        min_similarity: float = DEFAULT_BM25_MIN_SIMILARITY,
        max_candidates: int | None = None,
    ) -> list[RetrievalResult]:
        """Recall documents by normalized BM25 similarity.

        The query-specific best BM25 score is normalized to ``1.0`` and other
        positive scores are expressed as a ratio of that best match.  Recall is
        selected by ``min_similarity`` rather than a fixed TopK.

        ``top_k`` remains as a compatibility-only post-threshold safety cap for
        older direct callers.  Production hybrid retrieval passes ``top_k=None``
        and uses ``max_candidates`` solely as an operational memory bound.
        """

        query_tokens = self.tokenize(query)
        allowed = {chunk.chunk_id for chunk in filter_chunks(self.chunks, metadata_filter)}
        scored: list[tuple[RagChunk, float]] = []
        for chunk, doc_tokens in zip(self.chunks, self.tokenized_docs):
            if chunk.chunk_id not in allowed:
                continue
            score = self._score_tokens(query_tokens, doc_tokens)
            if score > 0:
                scored.append((chunk, float(score)))
        if not scored:
            return []

        scored.sort(key=lambda item: item[1], reverse=True)
        best_score = max(scored[0][1], 1e-12)
        threshold = min(1.0, max(0.0, float(min_similarity)))
        rows: list[RetrievalResult] = []
        for chunk, score in scored:
            similarity = min(1.0, max(0.0, score / best_score))
            if similarity < threshold:
                continue
            metadata = {
                **chunk.to_dict(),
                "bm25_similarity": float(similarity),
                "bm25_similarity_threshold": float(threshold),
            }
            rows.append(
                RetrievalResult(
                    chunk_id=chunk.chunk_id,
                    news_id=chunk.news_id,
                    chunk_text=chunk.chunk_text,
                    bm25_score=score,
                    metadata=metadata,
                )
            )

        limit = max_candidates
        if limit is None and top_k is not None:
            limit = top_k
        if limit is not None:
            rows = rows[: max(1, int(limit))]
        return rows

    def save_index(self, path: str | Path) -> Path:
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as f:
            pickle.dump(self, f)
        return out_path

    @classmethod
    def load_index(cls, path: str | Path) -> "BM25Retriever":
        with Path(path).open("rb") as f:
            return pickle.load(f)
