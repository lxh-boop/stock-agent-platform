from __future__ import annotations

from typing import Any

from .memory_store_contract import MemoryStore
from .memory_store_factory import (
    MemoryStoreBackendNotConfigured,
    create_memory_store,
    register_memory_store_backend,
    registered_memory_store_backends,
)


class GraphMemoryStore:
    """Graph-memory capability placeholder; not the primary persistence store."""

    def available(self) -> bool:
        return False

    def upsert_node(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "GraphMemoryStore is an interface placeholder; no graph backend is configured."
        )

    def query(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "GraphMemoryStore is an interface placeholder; no graph backend is configured."
        )


class VectorMemoryStore:
    """Vector-memory capability placeholder; not the primary persistence store."""

    def available(self) -> bool:
        return False

    def upsert_vector(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "VectorMemoryStore is an interface placeholder; no vector backend is configured."
        )

    def query(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "VectorMemoryStore is an interface placeholder; no vector backend is configured."
        )


__all__ = [
    "GraphMemoryStore",
    "MemoryStore",
    "MemoryStoreBackendNotConfigured",
    "VectorMemoryStore",
    "create_memory_store",
    "register_memory_store_backend",
    "registered_memory_store_backends",
]
