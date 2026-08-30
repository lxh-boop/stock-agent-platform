from agent.memory.memory_manager import MemoryManager
from agent.memory.in_memory_memory_store import InMemoryMemoryStore
from agent.memory.memory_types import MemoryStatus, MemoryType


def test_candidate_persists_via_store_contract_and_is_not_retrieved():
    store = InMemoryMemoryStore()
    manager = MemoryManager(store=store)
    candidates = manager.remember_candidate(
        "以后请记住我偏好稳健投资",
        user_id="u1",
        ttl_seconds=3600,
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.status == MemoryStatus.CANDIDATE
    assert candidate.memory_type == MemoryType.SEMANTIC

    reloaded = MemoryManager(store=store)
    pending = reloaded.list_candidates(user_id="u1")
    assert [item.memory_id for item in pending] == [candidate.memory_id]
    assert reloaded.retrieve_for_context(
        user_id="u1",
        query="稳健投资",
        relevance_threshold=0.0,
    )["items"] == []

    active = reloaded.confirm_candidate(candidate.memory_id, user_id="u1")
    assert active.status == MemoryStatus.ACTIVE
    assert active.metadata["user_confirmed"] is True
    assert reloaded.retrieve_for_context(
        user_id="u1",
        query="稳健投资",
        relevance_threshold=0.0,
    )["items"]
