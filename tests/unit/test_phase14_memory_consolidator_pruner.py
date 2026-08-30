from __future__ import annotations

from datetime import datetime, timedelta

from agent.memory import (
    MemoryConsolidator,
    MemoryPruner,
    MemoryRecord,
    MemoryScope,
    MemoryType,
)
from agent.memory.in_memory_memory_store import InMemoryMemoryStore


def _record(content: str, *, importance: float = 0.5, memory_id: str = "", valid_until: str = "") -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        user_id="u1",
        memory_type=MemoryType.SEMANTIC,
        memory_subtype="preference",
        scope=MemoryScope.USER,
        source_type="confirmed_user_preference",
        source_id=memory_id or content,
        content=content,
        topics=["risk"],
        stock_codes=["600519"],
        importance=importance,
        confidence=0.8,
        valid_until=valid_until,
        metadata={"user_confirmed": True},
    )


def test_phase14_memory_consolidator_merges_duplicate_groups() -> None:
    consolidator = MemoryConsolidator()
    first = _record("Prefer lower drawdown.", importance=0.7, memory_id="mem_first")
    second = _record("Prefer lower drawdown with evidence.", importance=0.9, memory_id="mem_second")
    second.source_refs = [{"source_id": "msg_2"}]

    merged = consolidator.consolidate([first, second])

    assert len(merged) == 1
    assert merged[0].memory_id == "mem_second"
    assert merged[0].metadata["consolidated_from"] == ["mem_first"]
    assert merged[0].source_refs == [{"source_id": "msg_2"}]


def test_phase14_memory_consolidator_store_soft_deletes_superseded() -> None:
    store = InMemoryMemoryStore()
    first = store.upsert(_record("Prefer lower drawdown.", importance=0.7, memory_id="mem_first"))
    second = store.upsert(_record("Prefer lower drawdown with evidence.", importance=0.9, memory_id="mem_second"))

    result = MemoryConsolidator().consolidate_store(store, user_id="u1")

    assert result["written"] == 1
    assert result["soft_deleted"] == 1
    assert store.get(first.memory_id, user_id="u1") is None
    assert store.get(second.memory_id, user_id="u1") is not None


def test_phase14_memory_pruner_removes_expired_and_low_importance() -> None:
    store = InMemoryMemoryStore()
    expired_time = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    expired = store.upsert(_record("expired memory", importance=0.8, memory_id="mem_expired", valid_until=expired_time))
    low = store.upsert(_record("low importance memory", importance=0.01, memory_id="mem_low"))
    kept = store.upsert(_record("important memory", importance=0.9, memory_id="mem_kept"))

    result = MemoryPruner(min_importance=0.05, max_records_per_user=10).prune_store(store, user_id="u1")

    assert result["deleted_count"] == 2
    assert store.get(expired.memory_id, user_id="u1") is None
    assert store.get(low.memory_id, user_id="u1") is None
    assert store.get(kept.memory_id, user_id="u1") is not None
