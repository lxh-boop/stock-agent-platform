from __future__ import annotations

from datetime import datetime
from typing import Any

from .memory_importance import MemoryImportanceScorer
from .memory_policy import MemoryPolicy
from .memory_sanitizer import MemorySanitizer
from .memory_types import MemoryRecord, MemoryStatus, MemoryType, is_record_expired


class InMemoryMemoryStore:
    """Volatile adapter used for tests and local contract verification."""

    backend_name = "inmemory"

    def __init__(
        self,
        *,
        policy: MemoryPolicy | None = None,
        sanitizer: MemorySanitizer | None = None,
        importance_scorer: MemoryImportanceScorer | None = None,
    ) -> None:
        self.policy = policy or MemoryPolicy.default()
        self.sanitizer = sanitizer or MemorySanitizer(self.policy)
        self.importance_scorer = importance_scorer or MemoryImportanceScorer()
        self._records: dict[str, MemoryRecord] = {}

    def describe(self) -> dict[str, Any]:
        return {
            "backend_name": self.backend_name,
            "storage_kind": "memory",
            "available": True,
        }

    @staticmethod
    def _clone(record: MemoryRecord) -> MemoryRecord:
        return MemoryRecord.from_dict(record.to_dict())

    def upsert(self, record: MemoryRecord | dict[str, Any]) -> MemoryRecord:
        safe = self.sanitizer.sanitize_record(record)
        self.policy.assert_can_store(safe)
        if safe.importance <= 0.0:
            safe.importance = self.importance_scorer.score(safe)
        stored = self._clone(safe)
        self._records[stored.memory_id] = stored
        return self._clone(stored)

    def get(
        self, memory_id: str, *, user_id: str = "", include_deleted: bool = False
    ) -> MemoryRecord | None:
        record = self._records.get(str(memory_id or ""))
        if record is None:
            return None
        if user_id and record.user_id != str(user_id):
            return None
        if not include_deleted and record.status == MemoryStatus.DELETED:
            return None
        return self._clone(record)

    def delete(
        self, memory_id: str, *, user_id: str = "", hard: bool = False
    ) -> bool:
        record = self.get(memory_id, user_id=user_id, include_deleted=True)
        if record is None:
            return False
        if hard:
            self._records.pop(record.memory_id, None)
            return True
        record.status = MemoryStatus.DELETED
        record.updated_at = datetime.now().isoformat(timespec="seconds")
        self._records[record.memory_id] = self._clone(record)
        return True

    def list_records(
        self,
        *,
        user_id: str = "",
        memory_types: list[MemoryType | str] | None = None,
        status: MemoryStatus | str | None = MemoryStatus.ACTIVE,
        topics: list[str] | None = None,
        stock_codes: list[str] | None = None,
        min_importance: float = 0.0,
        created_after: str = "",
        created_before: str = "",
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        allowed_types = {MemoryType.from_value(item) for item in (memory_types or [])}
        expected_status = MemoryStatus.from_value(status) if status is not None else None
        topic_set = {str(item).lower() for item in (topics or [])}
        stock_set = {str(item).split(".")[0].zfill(6) for item in (stock_codes or [])}
        rows: list[MemoryRecord] = []
        for record in self._records.values():
            if user_id and record.user_id != str(user_id):
                continue
            if expected_status is not None and record.status != expected_status:
                continue
            if allowed_types and record.memory_type not in allowed_types:
                continue
            if created_after and record.created_at < str(created_after):
                continue
            if created_before and record.created_at > str(created_before):
                continue
            if not include_expired and is_record_expired(record):
                continue
            if record.importance < float(min_importance or 0.0):
                continue
            if topic_set and not (topic_set & {item.lower() for item in record.topics}):
                continue
            if stock_set and not (stock_set & set(record.stock_codes)):
                continue
            rows.append(self._clone(record))
        rows.sort(key=lambda item: item.updated_at, reverse=True)
        return rows[: max(1, int(limit or 100))]

    def count(
        self, *, user_id: str = "", status: MemoryStatus | str | None = None
    ) -> int:
        expected_status = MemoryStatus.from_value(status) if status is not None else None
        return sum(
            1
            for record in self._records.values()
            if (not user_id or record.user_id == str(user_id))
            and (expected_status is None or record.status == expected_status)
        )

    def set_status(
        self,
        memory_id: str,
        *,
        user_id: str,
        status: MemoryStatus | str,
        metadata_updates: dict[str, Any] | None = None,
        clear_expiry: bool = False,
    ) -> MemoryRecord | None:
        record = self.get(memory_id, user_id=user_id, include_deleted=True)
        if record is None:
            return None
        record.status = MemoryStatus.from_value(status)
        record.metadata = {**dict(record.metadata or {}), **dict(metadata_updates or {})}
        if clear_expiry:
            record.valid_until = ""
        record.updated_at = datetime.now().isoformat(timespec="seconds")
        self._records[record.memory_id] = self._clone(record)
        return self._clone(record)


__all__ = ["InMemoryMemoryStore"]
