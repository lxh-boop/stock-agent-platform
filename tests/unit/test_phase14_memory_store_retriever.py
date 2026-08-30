from __future__ import annotations

import json

import pytest

from agent.memory import (
    GraphMemoryStore,
    MemoryRecord,
    MemoryRetriever,
    MemoryScope,
    MemoryType,
    VectorMemoryStore,
)
from agent.memory.in_memory_memory_store import InMemoryMemoryStore
from agent.memory.memory_store_contract import MemoryStore


def _confirmed_preference(**kwargs) -> MemoryRecord:
    data = {
        "user_id": "u1",
        "memory_type": MemoryType.SEMANTIC,
        "memory_subtype": "preference",
        "scope": MemoryScope.USER,
        "source_type": "confirmed_user_preference",
        "source_id": "msg_1",
        "content": "Prefer lower drawdown explanations for 600519.",
        "topics": ["risk"],
        "stock_codes": ["600519"],
        "importance": 0.9,
        "confidence": 0.9,
        "metadata": {"user_confirmed": True},
    }
    data.update(kwargs)
    return MemoryRecord(**data)


def test_phase14_memory_store_contract_roundtrip_and_filters() -> None:
    store = InMemoryMemoryStore()
    assert isinstance(store, MemoryStore)
    record = store.upsert(_confirmed_preference())
    store.upsert(
        MemoryRecord(
            user_id="u2",
            memory_type=MemoryType.SEMANTIC,
            memory_subtype="preference",
            source_type="confirmed_user_preference",
            source_id="msg_2",
            content="u2 private 000001",
            stock_codes=["000001"],
            metadata={"user_confirmed": True},
        )
    )
    loaded = store.get(record.memory_id, user_id="u1")
    filtered = store.list_records(
        user_id="u1", memory_types=[MemoryType.SEMANTIC], topics=["risk"], stock_codes=["600519"]
    )
    assert loaded is not None
    assert loaded.memory_id == record.memory_id
    assert [item.memory_id for item in filtered] == [record.memory_id]
    assert store.count(user_id="u1") == 1
    assert "u2 private" not in str([item.to_dict() for item in filtered])


def test_phase14_memory_store_does_not_persist_secrets() -> None:
    store = InMemoryMemoryStore()
    stored = store.upsert(
        _confirmed_preference(
            content=r"api_key=abc prefer lower drawdown D:\stock_daily_app\data\agent_quant.db",
            metadata={
                "user_confirmed": True,
                "confirmation_token": "secret",
                "raw_evidence": [{"chunk_id": "c1", "text": "raw"}],
            },
        )
    )
    loaded = store.get(stored.memory_id, user_id="u1")
    encoded = json.dumps(loaded.to_dict(), ensure_ascii=False)
    assert "confirmation_token" not in encoded
    assert "api_key" not in encoded
    assert "agent_quant.db" not in encoded
    assert "raw_evidence" not in encoded
    assert loaded.metadata["evidence_summary"]["count"] == 1


def test_phase14_memory_retriever_uses_persistent_store_only() -> None:
    store = InMemoryMemoryStore()
    stored_record = store.upsert(_confirmed_preference())
    retriever = MemoryRetriever(store=store)
    results = retriever.retrieve(
        user_id="u1", query="600519 lower drawdown", stock_codes=["600519"], limit=5
    )
    assert results
    assert results[0].record.memory_id == stored_record.memory_id
    assert results[0].score_parts["entity"] == 1.0


def test_phase14_store_rejects_unconfirmed_long_term_preference() -> None:
    store = InMemoryMemoryStore()
    with pytest.raises(ValueError, match="long_term_user_fact_requires_confirmation"):
        store.upsert(
            MemoryRecord(
                user_id="u1",
                memory_type=MemoryType.SEMANTIC,
                memory_subtype="risk_preference",
                source_type="user_message",
                source_id="msg_unconfirmed",
                content="Prefer speculative stocks.",
            )
        )


def test_phase14_graph_and_vector_stores_are_placeholders() -> None:
    graph = GraphMemoryStore()
    vector = VectorMemoryStore()
    assert graph.available() is False
    assert vector.available() is False
    with pytest.raises(NotImplementedError):
        graph.query()
    with pytest.raises(NotImplementedError):
        vector.query()
