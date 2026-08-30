from __future__ import annotations

from collections import defaultdict
from typing import Any

from .memory_store import MemoryStore
from .memory_types import MemoryRecord


class MemoryConsolidator:
    def consolidate(self, records: list[MemoryRecord]) -> list[MemoryRecord]:
        groups: dict[tuple[str, str, str, tuple[str, ...], tuple[str, ...]], list[MemoryRecord]] = defaultdict(list)
        for record in records:
            key = (
                record.user_id,
                record.memory_type.value,
                record.memory_subtype,
                tuple(sorted(record.topics)),
                tuple(sorted(record.stock_codes)),
            )
            groups[key].append(record)
        consolidated: list[MemoryRecord] = []
        for group in groups.values():
            if len(group) == 1:
                consolidated.append(group[0])
                continue
            primary = max(group, key=lambda item: (item.importance, item.confidence, item.updated_at))
            merged = MemoryRecord.from_dict(primary.to_dict())
            merged.importance = max(item.importance for item in group)
            merged.confidence = max(item.confidence for item in group)
            merged.source_refs = _merge_refs(*(item.source_refs for item in group))
            merged.artifact_refs = _merge_refs(*(item.artifact_refs for item in group))
            merged.message_refs = _merge_refs(*(item.message_refs for item in group))
            merged.context_refs = _merge_refs(*(item.context_refs for item in group))
            merged.approval_refs = _merge_refs(*(item.approval_refs for item in group))
            merged.metadata = {
                **dict(merged.metadata or {}),
                "consolidated_from": [item.memory_id for item in group if item.memory_id != merged.memory_id],
                "consolidated_count": len(group),
            }
            consolidated.append(merged)
        return consolidated

    def consolidate_store(self, store: MemoryStore, *, user_id: str, limit: int = 500) -> dict[str, Any]:
        records = store.list_records(user_id=user_id, limit=limit)
        consolidated = self.consolidate(records)
        consolidated_ids = {record.memory_id for record in consolidated}
        deleted = 0
        written = 0
        for record in consolidated:
            if record.metadata.get("consolidated_from"):
                store.upsert(record)
                written += 1
                for old_id in record.metadata.get("consolidated_from") or []:
                    if old_id and old_id not in consolidated_ids and store.delete(str(old_id), user_id=user_id):
                        deleted += 1
        return {
            "input_count": len(records),
            "output_count": len(consolidated),
            "written": written,
            "soft_deleted": deleted,
        }


def _merge_refs(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for group in groups:
        for item in group or []:
            key = "|".join(f"{k}={v}" for k, v in sorted(dict(item).items()))
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    return merged[:50]
