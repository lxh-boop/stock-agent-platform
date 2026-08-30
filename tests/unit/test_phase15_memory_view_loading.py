from __future__ import annotations

import json

from agent.memory import (
    MemoryManager,
    MemoryScope,
    MemoryType,
    list_memory_records_safe_page,
)
from agent.memory.in_memory_memory_store import InMemoryMemoryStore
import agent.memory.memory_context_bridge as memory_bridge


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def test_phase15_memory_safe_page_is_paginated_and_redacted(monkeypatch) -> None:
    manager = MemoryManager(store=InMemoryMemoryStore())
    monkeypatch.setattr(memory_bridge, "get_memory_manager_for_output", lambda _output_dir="outputs": manager)
    for index in range(7):
        manager.remember(
            user_id="u1",
            content=(
                f"Preference {index}: prefer evidence-first answer token abc{index} "
                r"from D:\stock_daily_app\data\agent_quant.db"
            ),
            memory_type=MemoryType.SEMANTIC,
            memory_subtype="preference",
            scope=MemoryScope.USER,
            source_type="confirmed_user_preference",
            source_id=f"msg_{index}",
            topics=["risk", "ui"],
            stock_codes=["600519"],
            metadata={"user_confirmed": True, "confirmation_token": "raw-token"},
            user_confirmed=True,
        )

    page = list_memory_records_safe_page(user_id="u1", output_dir="ignored", limit=5, offset=0)
    encoded = _encoded(page)

    assert page["status"] == "ok"
    assert page["total_count"] == 7
    assert len(page["records"]) == 5
    assert page["records"][0]["summary"]
    assert "raw-token" not in encoded
    assert "confirmation_token" not in encoded
    assert "agent_quant.db" not in encoded
    assert r"D:\stock_daily_app" not in encoded


def test_phase15_memory_safe_page_handles_empty_store(monkeypatch) -> None:
    manager = MemoryManager(store=InMemoryMemoryStore())
    monkeypatch.setattr(memory_bridge, "get_memory_manager_for_output", lambda _output_dir="outputs": manager)
    page = list_memory_records_safe_page(user_id="missing", output_dir="ignored", limit=3, offset=10)
    encoded = _encoded(page)

    assert page["status"] == "ok"
    assert page["records"] == []
    assert page["offset"] == 10
    assert "confirmation_token" not in encoded
