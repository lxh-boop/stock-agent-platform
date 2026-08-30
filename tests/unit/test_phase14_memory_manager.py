from __future__ import annotations

import json

import pytest

from agent.memory import MemoryManager, MemoryRecord, MemoryScope, MemoryType
from agent.memory.in_memory_memory_store import InMemoryMemoryStore


def test_phase14_memory_manager_remember_retrieve_and_forget() -> None:
    manager = MemoryManager(store=InMemoryMemoryStore())
    record = manager.remember(
        user_id="u1",
        content="Prefer lower drawdown explanations for 600519.",
        memory_type=MemoryType.SEMANTIC,
        memory_subtype="preference",
        scope=MemoryScope.USER,
        source_type="confirmed_user_preference",
        source_id="msg_1",
        topics=["risk"],
        stock_codes=["600519"],
        metadata={"user_confirmed": True},
        user_confirmed=True,
    )

    context = manager.retrieve_for_context(user_id="u1", query="600519 drawdown", stock_codes=["600519"])

    assert context["items"]
    assert context["items"][0]["memory"]["memory_id"] == record.memory_id
    assert context["policy"]["memory_manager_has_no_commit_permission"] is True
    assert manager.forget(record.memory_id, user_id="u1") is True
    assert manager.retrieve(user_id="u1", query="600519") == []


def test_phase14_memory_manager_rejects_unconfirmed_long_term_user_fact() -> None:
    manager = MemoryManager(store=InMemoryMemoryStore())

    with pytest.raises(ValueError, match="long_term_user_fact_requires_confirmation"):
        manager.remember(
            user_id="u1",
            content="Prefer speculative high-volatility names.",
            memory_type=MemoryType.SEMANTIC,
            memory_subtype="risk_preference",
            source_type="user_message",
            source_id="msg_1",
        )


def test_phase14_memory_manager_candidates_are_persisted_but_not_retrieved() -> None:
    manager = MemoryManager(store=InMemoryMemoryStore())
    candidates = manager.remember_candidate("我更偏好稳健一点，记住这个偏好", user_id="u1")
    assert candidates
    assert manager.store.count(user_id="u1", status="CANDIDATE") == 1
    context = manager.retrieve_for_context(user_id="u1", query="稳健 偏好")
    assert context["items"] == []

def test_phase14_memory_manager_rejects_working_memory_persistence() -> None:
    manager = MemoryManager(store=InMemoryMemoryStore())
    with pytest.raises(ValueError, match="working_memory_removed_use_context_bundle_for_run_state"):
        manager.remember(
            MemoryRecord(
                user_id="u1",
                memory_type=MemoryType.WORKING,
                content="temporary run state",
            ),
            long_term=False,
        )

def test_phase14_memory_manager_has_no_commit_surface() -> None:
    manager = MemoryManager(store=InMemoryMemoryStore())

    assert not hasattr(manager, "commit")
    assert not hasattr(manager, "execute")
    assert not hasattr(manager, "write_portfolio_state")
