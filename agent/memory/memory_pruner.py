from __future__ import annotations

from typing import Any

from .memory_store import MemoryStore
from .memory_types import MemoryRecord, is_record_expired


class MemoryPruner:
    def __init__(self, *, min_importance: float = 0.05, max_records_per_user: int = 500) -> None:
        self.min_importance = float(min_importance)
        self.max_records_per_user = max(1, int(max_records_per_user or 500))

    def select_prunable(self, records: list[MemoryRecord]) -> list[MemoryRecord]:
        expired = [record for record in records if is_record_expired(record)]
        low_importance = [record for record in records if record.importance < self.min_importance and record not in expired]
        active_sorted = sorted(
            [record for record in records if record not in expired and record not in low_importance],
            key=lambda item: (item.importance, item.updated_at),
        )
        overflow = active_sorted[: max(0, len(active_sorted) - self.max_records_per_user)]
        return [*expired, *low_importance, *overflow]

    def prune_store(self, store: MemoryStore, *, user_id: str, hard: bool = False, limit: int = 2000) -> dict[str, Any]:
        records = store.list_records(user_id=user_id, include_expired=True, limit=limit)
        prunable = self.select_prunable(records)
        deleted = 0
        for record in prunable:
            if store.delete(record.memory_id, user_id=user_id, hard=hard):
                deleted += 1
        return {
            "input_count": len(records),
            "selected_count": len(prunable),
            "deleted_count": deleted,
            "hard_delete": bool(hard),
        }
