from __future__ import annotations

import os
from typing import Callable, Any

from .memory_store_contract import MemoryStore


class MemoryStoreBackendNotConfigured(RuntimeError):
    pass


MemoryStoreBuilder = Callable[..., MemoryStore]
_BACKENDS: dict[str, MemoryStoreBuilder] = {}


def register_memory_store_backend(
    name: str, builder: MemoryStoreBuilder, *, replace: bool = False
) -> None:
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("memory_store_backend_name_required")
    if key in _BACKENDS and not replace:
        raise ValueError(f"memory_store_backend_already_registered:{key}")
    _BACKENDS[key] = builder


def _ensure_builtin_backends() -> None:
    if "postgresql" not in _BACKENDS:
        from .postgres_memory_store import PostgresMemoryStore

        register_memory_store_backend("postgresql", PostgresMemoryStore)
    if "inmemory" not in _BACKENDS:
        from .in_memory_memory_store import InMemoryMemoryStore

        register_memory_store_backend("inmemory", InMemoryMemoryStore)


def create_memory_store(
    backend: str | None = None,
    **kwargs: Any,
) -> MemoryStore:
    """Create a MemoryStore without exposing backend details to callers.

    Third-party/document adapters can register a builder once and then be
    selected by STOCK_MEMORY_STORE_BACKEND without changing MemoryManager or
    any retrieval/consolidation code.
    """

    _ensure_builtin_backends()
    selected = str(
        backend or os.getenv("STOCK_MEMORY_STORE_BACKEND", "postgresql")
    ).strip().lower()
    builder = _BACKENDS.get(selected)
    if builder is None:
        raise MemoryStoreBackendNotConfigured(
            f"memory_store_backend_not_configured:{selected}"
        )
    store = builder(**kwargs)
    if not isinstance(store, MemoryStore):
        raise TypeError(f"memory_store_contract_not_satisfied:{selected}")
    return store


def registered_memory_store_backends() -> tuple[str, ...]:
    _ensure_builtin_backends()
    return tuple(sorted(_BACKENDS))


__all__ = [
    "MemoryStoreBackendNotConfigured",
    "create_memory_store",
    "register_memory_store_backend",
    "registered_memory_store_backends",
]
