"""Run-scoped concurrency gates for Request/Worker/Tool/LLM execution."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from core.config.service_settings import get_service_settings


@dataclass
class _Gate:
    name: str
    limit: int
    semaphore: threading.BoundedSemaphore = field(init=False, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    active: int = 0
    peak: int = 0

    def __post_init__(self) -> None:
        self.semaphore = threading.BoundedSemaphore(max(1, int(self.limit)))

    @contextmanager
    def slot(self) -> Iterator[None]:
        self.semaphore.acquire()
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            yield
        finally:
            with self.lock:
                self.active = max(0, self.active - 1)
            self.semaphore.release()

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {"limit": self.limit, "active": self.active, "peak": self.peak}


class RuntimeResourceBudget:
    """One shared budget across all Request branches in a parent Run."""

    def __init__(
        self,
        *,
        max_parallel_requests: int | None = None,
        max_parallel_workers: int | None = None,
        max_parallel_tools: int | None = None,
        max_parallel_llm: int | None = None,
    ) -> None:
        settings = get_service_settings()
        self.request_gate = _Gate("request", max_parallel_requests or settings.max_parallel_requests)
        self.worker_gate = _Gate("worker", max_parallel_workers or settings.max_parallel_workers)
        self.tool_gate = _Gate("tool", max_parallel_tools or settings.max_parallel_tools)
        self.llm_gate = _Gate("llm", max_parallel_llm or settings.max_parallel_llm)

    @property
    def max_parallel_requests(self) -> int:
        return self.request_gate.limit

    @property
    def max_parallel_workers(self) -> int:
        return self.worker_gate.limit

    @property
    def max_parallel_tools(self) -> int:
        return self.tool_gate.limit

    @contextmanager
    def request_slot(self) -> Iterator[None]:
        with self.request_gate.slot():
            yield

    @contextmanager
    def worker_slot(self) -> Iterator[None]:
        with self.worker_gate.slot():
            yield

    @contextmanager
    def tool_slot(self) -> Iterator[None]:
        with self.tool_gate.slot():
            yield

    @contextmanager
    def llm_slot(self) -> Iterator[None]:
        with self.llm_gate.slot():
            yield

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": "runtime_resource_budget.v1",
            "request": self.request_gate.snapshot(),
            "worker": self.worker_gate.snapshot(),
            "tool": self.tool_gate.snapshot(),
            "llm": self.llm_gate.snapshot(),
        }


class LLMConcurrencyGate:
    """Transparent LLMService proxy with one parent-run concurrency gate."""

    def __init__(self, service: Any, budget: RuntimeResourceBudget) -> None:
        self._service = service
        self._budget = budget

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)

    def generate_text(self, **kwargs: Any) -> str:
        with self._budget.llm_slot():
            return self._service.generate_text(**kwargs)

    def generate_json(self, **kwargs: Any) -> dict[str, Any]:
        with self._budget.llm_slot():
            return self._service.generate_json(**kwargs)


__all__ = ["LLMConcurrencyGate", "RuntimeResourceBudget"]
