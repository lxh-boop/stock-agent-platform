from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .memory_types import MemoryRecord, MemoryStatus, MemoryType


@runtime_checkable
class MemoryStore(Protocol):
    """Backend-neutral persistence contract for long-term Agent memory.

    Upper layers must depend only on this protocol. SQL, file paths, collection
    names and connection objects belong to backend adapters.
    """

    backend_name: str

    def upsert(self, record: MemoryRecord | dict[str, Any]) -> MemoryRecord: ...

    def get(
        self, memory_id: str, *, user_id: str = "", include_deleted: bool = False
    ) -> MemoryRecord | None: ...

    def delete(
        self, memory_id: str, *, user_id: str = "", hard: bool = False
    ) -> bool: ...

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
    ) -> list[MemoryRecord]: ...

    def count(
        self, *, user_id: str = "", status: MemoryStatus | str | None = None
    ) -> int: ...

    def set_status(
        self,
        memory_id: str,
        *,
        user_id: str,
        status: MemoryStatus | str,
        metadata_updates: dict[str, Any] | None = None,
        clear_expiry: bool = False,
    ) -> MemoryRecord | None: ...

    def describe(self) -> dict[str, Any]: ...


__all__ = ["MemoryStore"]
