from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from database.repositories import AgentRepository


RUN_CREATED = "created"
RUN_PLANNING = "planning"
RUN_RUNNING = "running"
RUN_OBSERVING = "observing"
RUN_REPLANNING = "replanning"
RUN_WAITING_FOR_APPROVAL = "waiting_for_approval"
RUN_REVALIDATING = "revalidating"
RUN_COMMITTING = "committing"
RUN_COMPLETED = "completed"
RUN_PARTIALLY_COMPLETED = "partially_completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_EXPIRED = "expired"

STEP_PENDING = "pending"
STEP_READY = "ready"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_CANCELLED = "cancelled"


LEGAL_RUN_TRANSITIONS: dict[str, set[str]] = {
    RUN_CREATED: {RUN_PLANNING, RUN_CANCELLED, RUN_FAILED},
    RUN_PLANNING: {RUN_RUNNING, RUN_WAITING_FOR_APPROVAL, RUN_FAILED, RUN_CANCELLED},
    RUN_RUNNING: {
        RUN_OBSERVING,
        RUN_REVALIDATING,
        RUN_WAITING_FOR_APPROVAL,
        RUN_COMPLETED,
        RUN_PARTIALLY_COMPLETED,
        RUN_FAILED,
        RUN_CANCELLED,
    },
    RUN_OBSERVING: {RUN_REPLANNING, RUN_WAITING_FOR_APPROVAL, RUN_COMPLETED, RUN_PARTIALLY_COMPLETED, RUN_FAILED},
    RUN_REPLANNING: {RUN_RUNNING, RUN_OBSERVING, RUN_COMPLETED, RUN_PARTIALLY_COMPLETED, RUN_FAILED},
    RUN_WAITING_FOR_APPROVAL: {RUN_REVALIDATING, RUN_CANCELLED, RUN_EXPIRED, RUN_FAILED},
    RUN_REVALIDATING: {RUN_COMMITTING, RUN_FAILED, RUN_CANCELLED, RUN_EXPIRED},
    RUN_COMMITTING: {RUN_COMPLETED, RUN_PARTIALLY_COMPLETED, RUN_FAILED},
    RUN_COMPLETED: set(),
    RUN_PARTIALLY_COMPLETED: set(),
    RUN_FAILED: set(),
    RUN_CANCELLED: set(),
    RUN_EXPIRED: set(),
}

LEGAL_STEP_TRANSITIONS: dict[str, set[str]] = {
    STEP_PENDING: {STEP_READY, STEP_RUNNING, STEP_SKIPPED, STEP_CANCELLED, STEP_FAILED},
    STEP_READY: {STEP_RUNNING, STEP_SKIPPED, STEP_CANCELLED, STEP_FAILED},
    STEP_RUNNING: {STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED, STEP_CANCELLED},
    STEP_SUCCEEDED: set(),
    STEP_FAILED: set(),
    STEP_SKIPPED: set(),
    STEP_CANCELLED: set(),
}

SENSITIVE_KEYS = {
    "api_key",
    "token",
    "confirmation_token",
    "confirmation_token_hash",
    "llm_api_key",
    "tushare_token",
    "password",
    "secret",
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_run_id() -> str:
    return f"agent_run_{uuid4().hex[:12]}"


def _json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def sanitize_payload(value: Any, max_chars: int = 800) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if text_key.lower() in SENSITIVE_KEYS:
                out[text_key] = "***"
            else:
                out[text_key] = sanitize_payload(item, max_chars=max_chars)
        return out
    if isinstance(value, list):
        items = [sanitize_payload(item, max_chars=max_chars) for item in value[:20]]
        if len(value) > 20:
            items.append({"truncated_count": len(value) - 20})
        return items
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "...[truncated]"
    return value


def summarize_result(result: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(result or {})
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    summary: dict[str, Any] = {
        "success": bool(data.get("success")),
        "message": str(data.get("message") or "")[:300],
        "warnings": sanitize_payload(data.get("warnings") or []),
        "errors": sanitize_payload(data.get("errors") or []),
    }
    if isinstance(payload, dict):
        for key in [
            "status",
            "stock_code",
            "stock_name",
            "position_count",
            "order_count",
            "total_count",
            "returned_count",
            "event_count",
            "plan_id",
            "execution_status",
            "confirmation_status",
        ]:
            if key in payload:
                summary[key] = sanitize_payload(payload.get(key))
        for key in ["records", "positions", "orders", "events", "chunks", "items", "order_ids"]:
            value = payload.get(key)
            if isinstance(value, list):
                summary[f"{key}_count"] = len(value)
    return summary


def source_records_from_result(
    *,
    run_id: str,
    step_id: str | None,
    tool_call_id: str,
    user_id: str,
    tool_name: str,
    result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    data = dict(result or {}).get("data")
    if not isinstance(data, dict):
        data = dict(result or {})
    sources: list[dict[str, Any]] = []

    def add(
        source_type: str,
        title: str,
        record_id: str = "",
        path: str = "",
        snippet: str = "",
        metadata_extra: dict[str, Any] | None = None,
    ) -> None:
        if len(sources) >= 10:
            return
        source_id = f"src_{uuid4().hex[:12]}"
        sources.append(
            {
                "source_id": source_id,
                "run_id": run_id,
                "message_id": None,
                "tool_call_id": tool_call_id,
                "user_id": user_id,
                "source_type": source_type,
                "source_title": str(title or tool_name)[:200],
                "database_record_id": str(record_id or ""),
                "file_path": str(path or ""),
                "content_hash": _json_hash({"title": title, "record_id": record_id, "path": path, "snippet": snippet})[:24],
                "retrieved_at": now_text(),
                "snippet": str(snippet or "")[:500],
                "metadata": {"step_id": step_id, "tool_name": tool_name, **dict(metadata_extra or {})},
            }
        )

    for row in data.get("records") or []:
        if isinstance(row, dict):
            add("ranking_record", row.get("stock_name") or row.get("stock_code") or "ranking", row.get("stock_code"), snippet=row.get("analysis_conclusion") or row.get("reason") or "")
    for row in data.get("positions") or []:
        if isinstance(row, dict):
            add("portfolio_position", row.get("stock_name") or row.get("stock_code") or "position", row.get("stock_code"), snippet=f"quantity={row.get('quantity')}, weight={row.get('position_ratio')}")
    for row in data.get("events") or []:
        if isinstance(row, dict):
            add("news_event", row.get("title") or "news", row.get("news_id"), snippet=row.get("summary") or row.get("content") or "")
    for row in data.get("chunks") or []:
        if isinstance(row, dict):
            add("rag_chunk", row.get("section_title") or row.get("chunk_id") or "chunk", row.get("chunk_id"), snippet=row.get("chunk_text") or "")
    for row in data.get("mcp_sources") or []:
        if isinstance(row, dict):
            metadata_title = row.get("title") or row.get("source_id") or "mcp_evidence"
            add(
                "mcp_evidence",
                metadata_title,
                row.get("source_id") or row.get("record_id") or "",
                snippet=row.get("snippet") or row.get("summary") or "",
                metadata_extra={
                    "provider_type": "mcp",
                    "server_id": row.get("server_id"),
                    "mcp_tool_name": row.get("tool_name"),
                    "retrieved_at": row.get("retrieved_at"),
                },
            )
    output_paths = data.get("output_paths")
    if isinstance(output_paths, dict):
        for name, path in output_paths.items():
            add("file", str(name), path=str(path))
    return sources


class AgentRuntimeRecorder:
    def __init__(
        self,
        *,
        user_id: str,
        goal: str,
        db_path: str | Path | None,
        session_id: str = "",
        run_id: str | None = None,
    ) -> None:
        self.user_id = str(user_id or "default")
        self.goal = str(goal or "")
        self.db_path = db_path
        self.session_id = str(session_id or "")
        self.run_id = run_id or make_run_id()
        self.repo = AgentRepository(db_path)
        self.conversation_id = self._resolve_conversation_id()
        existing_run = (
            self.repo._decode_runtime_record(
                "agent_runs",
                self.repo.store.get("agent_runs", {"run_id": self.run_id}),
            )
            if run_id
            else None
        )
        if existing_run:
            self.status = str(existing_run.get("status") or RUN_CREATED)
            metadata = existing_run.get("metadata_json")
            if not isinstance(metadata, dict):
                metadata = existing_run.get("metadata")
            self._run_metadata = dict(metadata or {})
            self._run_metadata.setdefault("session_id", self.session_id)
            self._run_metadata.setdefault("status_transitions", [])
            self.repo.upsert_agent_run(
                {
                    "run_id": self.run_id,
                    "conversation_id": self.conversation_id,
                    "user_id": self.user_id,
                    "goal": self.goal[:500],
                    "status": self.status,
                    "metadata": self._run_metadata,
                }
            )
            return

        self.status = RUN_CREATED
        self._run_metadata: dict[str, Any] = {
            "session_id": self.session_id,
            "status_transitions": [
                {"from": "", "to": RUN_CREATED, "at": now_text(), "reason": "run_created"}
            ],
        }
        self.repo.upsert_agent_run(
            {
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "user_id": self.user_id,
                "goal": self.goal[:500],
                "status": RUN_CREATED,
                "created_at": now_text(),
                "metadata": self._run_metadata,
            }
        )

    def _resolve_conversation_id(self) -> str | None:
        if not self.session_id:
            return None
        try:
            if self.repo.store.get("conversations", {"conversation_id": self.session_id}):
                return self.session_id
        except Exception:
            return None
        return None

    def transition_run(self, to_status: str, reason: str = "") -> None:
        if to_status == self.status:
            return
        allowed = LEGAL_RUN_TRANSITIONS.get(self.status, set())
        if to_status not in allowed:
            raise ValueError(f"illegal_run_transition:{self.status}->{to_status}")
        previous = self.status
        self.status = to_status
        self._run_metadata.setdefault("status_transitions", []).append(
            {"from": previous, "to": to_status, "at": now_text(), "reason": str(reason or "")}
        )
        changes: dict[str, Any] = {
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "goal": self.goal[:500],
            "status": to_status,
            "metadata": self._run_metadata,
        }
        if to_status == RUN_RUNNING:
            changes["started_at"] = now_text()
        if to_status in {RUN_COMPLETED, RUN_PARTIALLY_COMPLETED, RUN_FAILED, RUN_CANCELLED, RUN_EXPIRED}:
            changes["finished_at"] = now_text()
        self.repo.upsert_agent_run(changes)

    def merge_metadata(self, updates: dict[str, Any]) -> None:
        if not updates:
            return
        for key, value in sanitize_payload(updates, max_chars=1200).items():
            if isinstance(value, dict) and isinstance(self._run_metadata.get(key), dict):
                self._run_metadata[key] = {**self._run_metadata[key], **value}
            else:
                self._run_metadata[key] = value
        self.repo.upsert_agent_run(
            {
                "run_id": self.run_id,
                "conversation_id": self.conversation_id,
                "user_id": self.user_id,
                "goal": self.goal[:500],
                "status": self.status,
                "metadata": self._run_metadata,
            }
        )

    def create_step(self, step_id: str, intent: str, depends_on: list[str] | None = None, status: str = STEP_PENDING, metadata: dict[str, Any] | None = None) -> None:
        self.repo.upsert_agent_step(
            {
                "step_id": step_id,
                "run_id": self.run_id,
                "intent": intent,
                "status": status,
                "depends_on": depends_on or [],
                "metadata": {
                    **dict(metadata or {}),
                    "status_transitions": [
                        {"from": "", "to": status, "at": now_text(), "reason": "step_created"}
                    ],
                },
            }
        )

    def transition_step(
        self,
        step_id: str,
        to_status: str,
        *,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        existing = self.repo._decode_runtime_record(
            "agent_steps",
            self.repo.store.get(
                "agent_steps",
                {"run_id": self.run_id, "step_id": str(step_id)},
            ),
        )
        if not existing:
            raise ValueError(f"unknown_agent_step:{self.run_id}:{step_id}")

        previous_status = str(existing.get("status") or STEP_PENDING)
        if to_status != previous_status:
            allowed = LEGAL_STEP_TRANSITIONS.get(previous_status, set())
            if to_status not in allowed:
                raise ValueError(
                    f"illegal_step_transition:{previous_status}->{to_status}"
                )

        existing_metadata = dict(existing.get("metadata_json") or {})
        transitions = list(existing_metadata.get("status_transitions") or [])
        if to_status != previous_status:
            transitions.append(
                {
                    "from": previous_status,
                    "to": to_status,
                    "at": now_text(),
                    "reason": str(reason or "step_transition"),
                }
            )
        existing_metadata.update(
            sanitize_payload(dict(metadata or {}), max_chars=1200)
        )
        existing_metadata["status_transitions"] = transitions

        changes: dict[str, Any] = {
            "step_id": str(step_id),
            "run_id": self.run_id,
            "status": to_status,
            "metadata": existing_metadata,
        }
        if to_status == STEP_RUNNING and not existing.get("started_at"):
            changes["started_at"] = now_text()
        if to_status in {
            STEP_SUCCEEDED,
            STEP_FAILED,
            STEP_SKIPPED,
            STEP_CANCELLED,
        }:
            changes["finished_at"] = now_text()
        self.repo.upsert_agent_step(changes)

    def record_step_result(self, step_id: str, result: dict[str, Any]) -> None:
        status = str(result.get("step_status") or (STEP_SUCCEEDED if result.get("success") else STEP_FAILED))
        started_at = str(result.get("started_at") or "")
        finished_at = str(result.get("finished_at") or now_text())
        existing = self.repo._decode_runtime_record(
            "agent_steps",
            self.repo.store.get("agent_steps", {"run_id": self.run_id, "step_id": step_id}),
        ) or {}
        metadata = dict(existing.get("metadata_json") or {})
        transitions = list(metadata.get("status_transitions") or [])
        previous_status = str(existing.get("status") or STEP_PENDING)
        if status != previous_status:
            if previous_status in {STEP_PENDING, STEP_READY} and status in {
                STEP_SUCCEEDED,
                STEP_FAILED,
                STEP_SKIPPED,
                STEP_CANCELLED,
            }:
                transitions.append(
                    {
                        "from": previous_status,
                        "to": STEP_RUNNING,
                        "at": started_at or now_text(),
                        "reason": "step_started",
                    }
                )
                transitions.append(
                    {
                        "from": STEP_RUNNING,
                        "to": status,
                        "at": finished_at,
                        "reason": "step_observed",
                    }
                )
            elif status in LEGAL_STEP_TRANSITIONS.get(previous_status, set()):
                transitions.append(
                    {
                        "from": previous_status,
                        "to": status,
                        "at": finished_at,
                        "reason": "step_observed",
                    }
                )
            else:
                transitions.append(
                    {
                        "from": previous_status,
                        "to": status,
                        "at": finished_at,
                        "reason": "step_observed_unmapped",
                    }
                )
        metadata.update(
            {
                "execution_mode": result.get("execution_mode"),
                "warnings": result.get("warnings") or [],
                "status_transitions": transitions,
            }
        )
        extra_metadata = result.get("metadata")
        if isinstance(extra_metadata, dict):
            metadata.update(sanitize_payload(extra_metadata, max_chars=1200))
        for key in [
            "agent_role",
            "message_id",
            "handoff_from",
            "handoff_to",
            "agent_input_summary",
            "agent_output_summary",
            "tool_calls_summary",
        ]:
            if key in result and result.get(key) not in (None, ""):
                metadata[key] = sanitize_payload(result.get(key), max_chars=1200)
        self.repo.upsert_agent_step(
            {
                "step_id": step_id,
                "run_id": self.run_id,
                "intent": str(result.get("intent") or ""),
                "status": status,
                "depends_on": result.get("depends_on") or [],
                "tool_args_summary": sanitize_payload(result.get("arguments") or {}),
                "observation_summary": str(result.get("message") or "")[:500],
                "error_type": ",".join(str(item) for item in (result.get("errors") or [])[:3]),
                "started_at": started_at or None,
                "finished_at": finished_at or None,
                "duration_seconds": float(result.get("duration_seconds") or 0.0),
                "metadata": metadata,
            }
        )

    def record_tool_call(
        self,
        *,
        step_id: str | None,
        tool_name: str,
        arguments: dict[str, Any] | None,
        result: dict[str, Any] | None,
        permission: str = "",
        started_at: str = "",
        finished_at: str = "",
        retry_count: int = 0,
        reliability: dict[str, Any] | None = None,
    ) -> str:
        tool_call_id = f"call_{uuid4().hex[:12]}"
        success = bool((result or {}).get("success"))
        errors = list((result or {}).get("errors") or [])
        sources = source_records_from_result(
            run_id=self.run_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            user_id=self.user_id,
            tool_name=tool_name,
            result=result,
        )
        artifact_ref: dict[str, Any] = {}
        try:
            from agent_graph.runtime.artifacts import save_tool_result_artifact

            artifact_ref = save_tool_result_artifact(
                db_path=self.db_path,
                output_dir=None,
                user_id=self.user_id,
                run_id=self.run_id,
                conversation_id=self.conversation_id or "",
                task_id=step_id or "",
                tool_name=tool_name,
                result=result,
                sources=sources,
            )
        except Exception as exc:
            artifact_ref = {"artifact_error": f"{type(exc).__name__}: {exc}"}
        reliability = sanitize_payload(dict(reliability or {}), max_chars=1200)
        mcp_metadata: dict[str, Any] = {}
        result_metadata = (
            (result or {}).get("metadata")
            if isinstance((result or {}).get("metadata"), dict)
            else {}
        )
        provider = (
            result_metadata.get("provider")
            if isinstance(result_metadata.get("provider"), dict)
            else {}
        )
        if str(provider.get("provider_type") or "") == "mcp":
            mcp_metadata = sanitize_payload(
                {
                    **provider,
                    "runtime_reliability": reliability,
                },
                max_chars=1200,
            )
        self.repo.upsert_agent_tool_call(
            {
                "tool_call_id": tool_call_id,
                "run_id": self.run_id,
                "step_id": step_id,
                "user_id": self.user_id,
                "tool_name": tool_name,
                "status": "success" if success else "failed",
                "input_summary": sanitize_payload(arguments or {}),
                "output_summary": summarize_result(result),
                "error_type": ",".join(str(item) for item in errors[:3]),
                "error_message": str((result or {}).get("message") or "")[:500] if errors else "",
                "started_at": started_at or now_text(),
                "finished_at": finished_at or now_text(),
                "duration_seconds": 0.0,
                "retry_count": int(retry_count or reliability.get("retry_count") or 0),
                "metadata": {
                    "permission": permission,
                    "source_ids": [source["source_id"] for source in sources],
                    "artifact_ids": [artifact_ref.get("artifact_id")] if artifact_ref.get("artifact_id") else [],
                    "artifact_ref": sanitize_payload(artifact_ref, max_chars=800),
                    "runtime_reliability": reliability,
                    **mcp_metadata,
                },
            }
        )
        for source in sources:
            self.repo.upsert_agent_source(source)
        return tool_call_id

    def attach_proposal(self, plan_id: str) -> None:
        if not plan_id:
            return
        try:
            self.repo.store.update(
                "action_proposals",
                {"plan_id": str(plan_id)},
                {"run_id": self.run_id, "status": "pending"},
            )
        except Exception:
            return


def load_run_snapshot(db_path: str | Path | None, run_id: str) -> dict[str, Any]:
    repo = AgentRepository(db_path)
    run = repo.store.get("agent_runs", {"run_id": run_id})
    if not run:
        return {}
    run = repo._decode_runtime_record("agent_runs", run) or {}
    steps = repo._list_runtime("agent_steps", filters={"run_id": run_id}, order_by="started_at")
    tool_calls = repo._list_runtime("agent_tool_calls", filters={"run_id": run_id}, order_by="started_at")
    sources = repo._list_runtime("agent_sources", filters={"run_id": run_id}, order_by="retrieved_at")
    proposals = repo._list_runtime("action_proposals", filters={"run_id": run_id}, order_by="created_at")
    plan_ids = [str(row.get("plan_id") or "") for row in proposals if row.get("plan_id")]
    approvals: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []
    for plan_id in plan_ids:
        approvals.extend(repo._list_runtime("action_approvals", filters={"plan_id": plan_id}, order_by="created_at"))
        commits.extend(repo._list_runtime("action_commits", filters={"plan_id": plan_id}, order_by="created_at"))

    metadata = run.get("metadata_json") if isinstance(run.get("metadata_json"), dict) else {}
    memory_refs: list[dict[str, Any]] = []
    memory_ids: set[str] = set()
    memory_used = metadata.get("memory_used") if isinstance(metadata, dict) else {}
    if isinstance(memory_used, dict):
        for phase, rows in memory_used.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                memory_id = str(row.get("memory_id") or "")
                if not memory_id or memory_id in memory_ids:
                    continue
                memory_ids.add(memory_id)
                memory_refs.append({"phase": phase, **sanitize_payload(row, max_chars=300)})
    memories: list[dict[str, Any]] = []
    for memory_id in sorted(memory_ids):
        memory = repo.get_memory_item(memory_id)
        if memory:
            memories.append(
                sanitize_payload(
                    {
                        "memory_id": memory.get("memory_id"),
                        "memory_type": memory.get("memory_type"),
                        "content": str(memory.get("content") or "")[:300],
                        "importance": memory.get("importance"),
                        "status": memory.get("status"),
                        "source_type": memory.get("source_type"),
                        "source_id": memory.get("source_id"),
                        "metadata": memory.get("metadata_json") or {},
                    },
                    max_chars=500,
                )
            )

    decision_replay = {
        "run": {
            "run_id": run.get("run_id"),
            "goal": run.get("goal"),
            "status": run.get("status"),
            "status_transitions": (metadata or {}).get("status_transitions", []),
        },
        "used_memories": memory_refs,
        "memory_records": memories,
        "agent_handoffs": [
            {
                "step_id": row.get("step_id"),
                "agent_role": (row.get("metadata_json") or {}).get("agent_role"),
                "handoff_from": (row.get("metadata_json") or {}).get("handoff_from"),
                "handoff_to": (row.get("metadata_json") or {}).get("handoff_to"),
                "status": row.get("status"),
            }
            for row in steps
            if isinstance(row.get("metadata_json"), dict)
            and (row.get("metadata_json") or {}).get("agent_role")
        ],
        "evidence": [
            {
                "source_id": row.get("source_id"),
                "source_type": row.get("source_type"),
                "title": row.get("source_title"),
                "record_id": row.get("database_record_id") or row.get("file_path"),
            }
            for row in sources
        ],
        "proposals": sanitize_payload(proposals),
        "approvals": sanitize_payload(approvals),
        "commits": sanitize_payload(commits),
        "final_result": {
            "finished_at": run.get("finished_at"),
            "approval_closure": (metadata or {}).get("approval_closure", {}),
        },
    }
    return {
        "run": run,
        "steps": steps,
        "tool_calls": tool_calls,
        "sources": sources,
        "proposals": proposals,
        "approvals": approvals,
        "commits": commits,
        "memories": memories,
        "decision_replay": decision_replay,
    }


def compare_run_decisions(
    db_path: str | Path | None,
    left_run_id: str,
    right_run_id: str,
) -> dict[str, Any]:
    left = load_run_snapshot(db_path, left_run_id).get("decision_replay") or {}
    right = load_run_snapshot(db_path, right_run_id).get("decision_replay") or {}

    def ids(rows: list[dict[str, Any]], key: str) -> set[str]:
        return {str(row.get(key) or "") for row in rows if row.get(key)}

    left_memories = ids(left.get("used_memories") or [], "memory_id")
    right_memories = ids(right.get("used_memories") or [], "memory_id")
    left_plans = ids(left.get("proposals") or [], "plan_id")
    right_plans = ids(right.get("proposals") or [], "plan_id")
    return {
        "left_run_id": left_run_id,
        "right_run_id": right_run_id,
        "left_status": (left.get("run") or {}).get("status"),
        "right_status": (right.get("run") or {}).get("status"),
        "differences": {
            "memory_only_left": sorted(left_memories - right_memories),
            "memory_only_right": sorted(right_memories - left_memories),
            "proposal_only_left": sorted(left_plans - right_plans),
            "proposal_only_right": sorted(right_plans - left_plans),
            "status_changed": (left.get("run") or {}).get("status") != (right.get("run") or {}).get("status"),
        },
    }
