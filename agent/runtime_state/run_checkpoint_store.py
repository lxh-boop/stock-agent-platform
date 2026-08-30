"""Persistent checkpoints for pause/resume without blocked Worker threads."""

from __future__ import annotations

from contextlib import contextmanager
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from database.connection import get_connection
from database.runtime_schema import ensure_runtime_schema


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunCheckpoint:
    run_id: str
    session_id: str
    user_id: str
    status: Literal["running", "waiting_user_input", "waiting_context", "waiting_approval", "completed", "failed", "cancelled"]
    current_node_id: str = ""
    blocked_task_id: str = ""
    capability_plan: dict[str, Any] = field(default_factory=dict)
    task_states: dict[str, str] = field(default_factory=dict)
    resolved_entity_refs: list[dict[str, Any]] = field(default_factory=list)
    data_refs: list[str] = field(default_factory=list)
    working_memory_snapshot: dict[str, Any] = field(default_factory=dict)
    missing_parameters: list[str] = field(default_factory=list)
    missing_context: list[str] = field(default_factory=list)
    pending_proposal_id: str = ""
    retry_count: int = 0
    replan_count: int = 0
    version: int = 1
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RequestCheckpoint:
    run_id: str
    request_id: str
    status: str
    current_phase: str = ""
    resolved_graph_refs: list[dict[str, Any]] = field(default_factory=list)
    worker_tasks: list[dict[str, Any]] = field(default_factory=list)
    missing_parameters: list[str] = field(default_factory=list)
    missing_context: list[str] = field(default_factory=list)
    replan_count: int = 0
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunCheckpointStore:
    def __init__(self, output_dir: str | Path) -> None:
        del output_dir
        self._lock = threading.RLock()
        ensure_runtime_schema()

    def _connect(self):
        return get_connection(runtime=True)

    @contextmanager
    def _connection(self) -> Iterator[object]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save(self, checkpoint: RunCheckpoint) -> RunCheckpoint:
        checkpoint.updated_at = _now()
        with self._lock, self._connection() as connection:
            previous = connection.execute(
                "SELECT version, created_at FROM agent_run_checkpoints WHERE run_id=%s",
                (checkpoint.run_id,),
            ).fetchone()
            if previous:
                checkpoint.version = int(previous["version"] or 0) + 1
                checkpoint.created_at = str(previous["created_at"])
            connection.execute(
                """
                INSERT INTO agent_run_checkpoints(run_id,session_id,user_id,status,checkpoint_json,version,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(run_id) DO UPDATE SET
                  session_id=excluded.session_id,user_id=excluded.user_id,status=excluded.status,
                  checkpoint_json=excluded.checkpoint_json,version=excluded.version,updated_at=excluded.updated_at
                """,
                (
                    checkpoint.run_id, checkpoint.session_id, checkpoint.user_id,
                    checkpoint.status, json.dumps(checkpoint.to_dict(), ensure_ascii=False, default=str),
                    checkpoint.version, checkpoint.created_at, checkpoint.updated_at,
                ),
            )
            connection.commit()
        return checkpoint

    @staticmethod
    def _checkpoint_from_json(raw_json: str) -> RunCheckpoint:
        payload = dict(json.loads(raw_json))
        # One-time persisted-checkpoint migration from pre-V23.0.16 field names.
        legacy_data_key = "slot" + "_refs"
        legacy_missing_key = "missing_context_" + "slots"
        if "data_refs" not in payload and legacy_data_key in payload:
            payload["data_refs"] = list(payload.pop(legacy_data_key) or [])
        if "missing_context" not in payload and legacy_missing_key in payload:
            payload["missing_context"] = list(payload.pop(legacy_missing_key) or [])
        return RunCheckpoint(**payload)

    def load(self, run_id: str) -> RunCheckpoint | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT checkpoint_json FROM agent_run_checkpoints WHERE run_id=%s", (str(run_id),)
            ).fetchone()
        return self._checkpoint_from_json(row["checkpoint_json"]) if row else None

    def pending_for_session(self, session_id: str) -> list[RunCheckpoint]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT checkpoint_json FROM agent_run_checkpoints WHERE session_id=%s AND status IN ('waiting_user_input','waiting_context','waiting_approval') ORDER BY updated_at DESC",
                (str(session_id),),
            ).fetchall()
        return [self._checkpoint_from_json(row["checkpoint_json"]) for row in rows]

    def save_request(self, checkpoint: RequestCheckpoint) -> RequestCheckpoint:
        checkpoint.updated_at = _now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO agent_request_checkpoints(run_id,request_id,status,checkpoint_json,updated_at)
                VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(run_id,request_id) DO UPDATE SET
                  status=excluded.status,checkpoint_json=excluded.checkpoint_json,updated_at=excluded.updated_at""",
                (checkpoint.run_id, checkpoint.request_id, checkpoint.status,
                 json.dumps(checkpoint.to_dict(), ensure_ascii=False, default=str), checkpoint.updated_at),
            )
        return checkpoint

    def requests_for_run(self, run_id: str) -> list[RequestCheckpoint]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT checkpoint_json FROM agent_request_checkpoints WHERE run_id=%s ORDER BY request_id",
                (str(run_id),),
            ).fetchall()
        return [RequestCheckpoint(**json.loads(row["checkpoint_json"])) for row in rows]
