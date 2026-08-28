"""Run 级 Worker 业务数据存储。

本模块只负责一次 Agent Run 内业务数据的“物理存储边界”。
ContextBundle 继续作为 Run 上下文的逻辑模型；Worker 不感知底层到底是
当前的进程内内存，还是未来的 RedisRunContextStore。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
import threading
from typing import Any


class RunContextStore(ABC):
    """一次 Run 内 Worker 共享业务数据的统一存储接口。"""

    @abstractmethod
    def put(self, item: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def items(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def has(self, *, entity_id: str, name: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def restore(self, snapshot: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def clear(self) -> None:
        raise NotImplementedError


class InMemoryRunContextStore(RunContextStore):
    """当前单进程 Runtime 使用的线程安全内存实现。"""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def put(self, item: dict[str, Any]) -> dict[str, Any]:
        row = deepcopy(dict(item or {}))
        with self._lock:
            self._items.append(row)
        return deepcopy(row)

    def items(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._items)

    def has(self, *, entity_id: str, name: str) -> bool:
        entity = str(entity_id or "__run__")
        data_name = str(name or "").strip()
        with self._lock:
            return any(
                str(item.get("entity_id") or "__run__") == entity
                and str(item.get("name") or "") == data_name
                for item in self._items
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": "run_context_store_snapshot.v1",
                "backend": "memory",
                "items": deepcopy(self._items),
            }

    def restore(self, snapshot: dict[str, Any]) -> None:
        rows = [dict(item) for item in list(dict(snapshot or {}).get("items") or []) if isinstance(item, dict)]
        with self._lock:
            self._items = deepcopy(rows)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


__all__ = ["RunContextStore", "InMemoryRunContextStore"]
