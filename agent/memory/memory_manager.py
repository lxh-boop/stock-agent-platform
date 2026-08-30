from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .memory_candidate_extractor import MemoryCandidateExtractor
from .memory_consolidator import MemoryConsolidator
from .memory_context_selector import MemoryContextSelector
from .memory_policy import MemoryPolicy
from .memory_pruner import MemoryPruner
from .memory_retrieval_types import MemoryRetrievalRequest
from .memory_retriever import MemoryRetriever
from .memory_sanitizer import MemorySanitizer
from .memory_store import MemoryStore, create_memory_store
from .memory_types import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    is_record_expired,
)


class MemoryManager:
    """Manage persistent candidate and long-term memory.

    Per-run working state is not stored here. ContextBundle is the single
    working-memory object for one user request / Agent run.
    """

    def __init__(
        self,
        *,
        store: MemoryStore | None = None,
        retriever: MemoryRetriever | None = None,
        selector: MemoryContextSelector | None = None,
        extractor: MemoryCandidateExtractor | None = None,
        consolidator: MemoryConsolidator | None = None,
        pruner: MemoryPruner | None = None,
        policy: MemoryPolicy | None = None,
        sanitizer: MemorySanitizer | None = None,
    ) -> None:
        self.policy = policy or MemoryPolicy.default()
        self.sanitizer = sanitizer or MemorySanitizer(self.policy)
        self.store = store or create_memory_store(
            policy=self.policy,
            sanitizer=self.sanitizer,
        )
        self.retriever = retriever or MemoryRetriever(store=self.store)
        self.selector = selector or MemoryContextSelector()
        self.extractor = extractor or MemoryCandidateExtractor(
            sanitizer=self.sanitizer
        )
        self.consolidator = consolidator or MemoryConsolidator()
        self.pruner = pruner or MemoryPruner()

    def remember(
        self,
        record: MemoryRecord | dict[str, Any] | str | None = None,
        *,
        user_id: str = "default_user",
        content: str = "",
        memory_type: MemoryType | str = MemoryType.EPISODIC,
        memory_subtype: str = "",
        source_type: str = "",
        source_id: str = "",
        conversation_id: str = "",
        run_id: str = "",
        task_id: str = "",
        scope: MemoryScope | str = MemoryScope.CONVERSATION,
        topics: list[str] | None = None,
        stock_codes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        user_confirmed: bool = False,
        long_term: bool | None = None,
        ttl_seconds: int | None = None,
    ) -> MemoryRecord:
        memory = _coerce_record(
            record,
            user_id=user_id,
            content=content,
            memory_type=memory_type,
            memory_subtype=memory_subtype,
            source_type=source_type,
            source_id=source_id,
            conversation_id=conversation_id,
            run_id=run_id,
            task_id=task_id,
            scope=scope,
            topics=topics,
            stock_codes=stock_codes,
            metadata=metadata,
        )
        if memory.memory_type == MemoryType.WORKING or long_term is False:
            raise ValueError(
                "working_memory_removed_use_context_bundle_for_run_state"
            )
        if user_confirmed:
            memory.metadata["user_confirmed"] = True
            if not memory.source_type:
                memory.source_type = "confirmed_user_preference"
        if ttl_seconds:
            memory.valid_until = _expiry_text(ttl_seconds)
        memory.status = MemoryStatus.ACTIVE
        safe = self.sanitizer.sanitize_record(memory)
        self.policy.assert_can_store(safe)
        return self.store.upsert(safe)

    def remember_candidate(
        self,
        value: Any,
        *,
        user_id: str = "default_user",
        source_type: str = "",
        ttl_seconds: int = 86400,
    ) -> list[MemoryRecord]:
        """Persist non-retrievable candidates in the configured MemoryStore until confirmed.

        Candidates keep their real memory type and use status=CANDIDATE. They
        are excluded from normal retrieval, survive process restarts, and expire
        automatically through valid_until.
        """

        candidates = self.extractor.extract(
            value,
            source_type=source_type,
            user_id=user_id,
        )
        stored: list[MemoryRecord] = []
        for candidate in candidates:
            if candidate.memory_type == MemoryType.WORKING:
                # Run state and pending approvals belong to ContextBundle and
                # the existing approval store, not to MemoryManager.
                continue
            candidate.status = MemoryStatus.CANDIDATE
            if not candidate.valid_until:
                candidate.valid_until = _expiry_text(ttl_seconds)
            candidate.metadata = {
                **dict(candidate.metadata or {}),
                "candidate_original_type": candidate.memory_type.value,
                "candidate_store": "persistent_memory_store",
                "working_state_owner": "context_bundle",
            }
            stored.append(self.store.upsert(candidate))
        return stored

    def list_candidates(
        self,
        *,
        user_id: str,
        include_expired: bool = False,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        return self.store.list_records(
            user_id=user_id,
            status=MemoryStatus.CANDIDATE,
            include_expired=include_expired,
            limit=limit,
        )

    def confirm_candidate(
        self,
        memory_id: str,
        *,
        user_id: str,
    ) -> MemoryRecord:
        candidate = self.store.get(
            memory_id,
            user_id=user_id,
            include_deleted=True,
        )
        if candidate is None or candidate.status != MemoryStatus.CANDIDATE:
            raise ValueError("memory_candidate_not_found")
        if is_record_expired(candidate):
            self.store.set_status(
                candidate.memory_id,
                user_id=user_id,
                status=MemoryStatus.EXPIRED,
            )
            raise ValueError("memory_candidate_expired")
        candidate.status = MemoryStatus.ACTIVE
        candidate.valid_until = ""
        candidate.metadata = {
            **dict(candidate.metadata or {}),
            "user_confirmed": True,
            "confirmed_at": _now_text(),
        }
        if candidate.source_type == "user_message":
            candidate.source_type = "confirmed_user_preference"
        self.policy.assert_can_store(candidate)
        return self.store.upsert(candidate)

    def reject_candidate(
        self,
        memory_id: str,
        *,
        user_id: str,
        reason: str = "",
    ) -> MemoryRecord:
        candidate = self.store.get(
            memory_id,
            user_id=user_id,
            include_deleted=True,
        )
        if candidate is None or candidate.status != MemoryStatus.CANDIDATE:
            raise ValueError("memory_candidate_not_found")
        candidate.status = MemoryStatus.REJECTED
        candidate.metadata = {
            **dict(candidate.metadata or {}),
            "rejected_at": _now_text(),
            "rejection_reason": str(reason or "")[:240],
        }
        return self.store.upsert(candidate)

    def expire_candidates(self, *, user_id: str, limit: int = 1000) -> int:
        expired_count = 0
        for candidate in self.list_candidates(
            user_id=user_id,
            include_expired=True,
            limit=limit,
        ):
            if not is_record_expired(candidate):
                continue
            updated = self.store.set_status(
                candidate.memory_id,
                user_id=user_id,
                status=MemoryStatus.EXPIRED,
                metadata_updates={"expired_at": _now_text()},
            )
            expired_count += int(updated is not None)
        return expired_count

    def retrieve(self, **kwargs: Any):
        return self.retriever.retrieve(**kwargs)

    def retrieve_for_context(
        self,
        *,
        request: MemoryRetrievalRequest | None = None,
        user_id: str = "",
        query: str = "",
        candidate_top_n: int = 40,
        relevance_threshold: float = 0.42,
        token_budget: int = 360,
        task_type: str = "",
        agent_role: str = "supervisor",
        entities: dict[str, Any] | None = None,
        topics: list[str] | None = None,
        stock_codes: list[str] | None = None,
        memory_types: list[MemoryType | str] | None = None,
        created_after: str = "",
        created_before: str = "",
        min_importance: float = 0.0,
        conversation_id: str = "",
        run_id: str = "",
        limit: int | None = None,
    ) -> dict[str, Any]:
        req = request or MemoryRetrievalRequest(
            user_id=user_id,
            query=query,
            conversation_id=conversation_id,
            run_id=run_id,
            task_type=task_type,
            agent_role=agent_role,
            entities=dict(entities or {}),
            topics=list(topics or []),
            stock_codes=list(stock_codes or []),
            memory_types=list(memory_types or []),
            created_after=created_after,
            created_before=created_before,
            candidate_top_n=int(limit or candidate_top_n),
            relevance_threshold=relevance_threshold,
            token_budget=token_budget,
            min_importance=min_importance,
        )
        req = req.normalized()
        candidates = self.retriever.retrieve(
            user_id=req.user_id,
            query=req.query,
            memory_types=req.memory_types or None,
            topics=req.topics or None,
            stock_codes=req.stock_codes or None,
            created_after=req.created_after,
            created_before=req.created_before,
            min_importance=req.min_importance,
            candidate_top_n=req.candidate_top_n,
        )
        selection = self.selector.select(candidates, req)

        safe_items: list[dict[str, Any]] = []
        for item in selection.selected:
            safe = dict(item)
            safe["memory"] = self.sanitizer.sanitize_for_llm(
                safe.get("memory") or {}
            )
            safe_items.append(safe)

        diagnostics = (
            selection.diagnostics.to_dict()
            if selection.diagnostics is not None
            else {}
        )
        return {
            "user_id": req.user_id,
            "query": req.query,
            "retrieval_id": req.retrieval_id,
            "items": safe_items,
            "diagnostics": diagnostics,
            "policy": {
                "secrets_removed": True,
                "long_term_user_facts_require_confirmation": True,
                "memory_manager_has_no_commit_permission": True,
                "working_memory_owner": "context_bundle_per_run",
                "candidate_store": "persistent_status_candidate",
                "context_admission": "relevance_threshold_then_entity_task_time_token_budget",
                "fixed_top_k_context_admission": False,
                "candidate_pool_top_n": req.candidate_top_n,
                "relevance_threshold": req.relevance_threshold,
                "token_budget": req.token_budget,
            },
        }

    def forget(self, memory_id: str, *, user_id: str, hard: bool = False) -> bool:
        return self.store.delete(memory_id, user_id=user_id, hard=hard)

    def consolidate(self, *, user_id: str, limit: int = 500) -> dict[str, Any]:
        return self.consolidator.consolidate_store(
            self.store,
            user_id=user_id,
            limit=limit,
        )

    def prune(
        self,
        *,
        user_id: str,
        hard: bool = False,
        limit: int = 2000,
    ) -> dict[str, Any]:
        expired_candidates = self.expire_candidates(
            user_id=user_id,
            limit=limit,
        )
        result = self.pruner.prune_store(
            self.store,
            user_id=user_id,
            hard=hard,
            limit=limit,
        )
        return {**result, "expired_candidates": expired_candidates}


def _coerce_record(
    record: MemoryRecord | dict[str, Any] | str | None,
    **defaults: Any,
) -> MemoryRecord:
    if isinstance(record, MemoryRecord):
        return MemoryRecord.from_dict({**defaults, **record.to_dict()})
    if isinstance(record, dict):
        return MemoryRecord.from_dict({**defaults, **record})
    if isinstance(record, str):
        defaults["content"] = record
    return MemoryRecord(**defaults)


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _expiry_text(ttl_seconds: int) -> str:
    ttl = max(1, int(ttl_seconds or 1))
    return (datetime.now() + timedelta(seconds=ttl)).isoformat(timespec="seconds")


__all__ = ["MemoryManager"]
