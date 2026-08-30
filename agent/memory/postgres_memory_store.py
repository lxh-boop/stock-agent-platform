from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from database.connection import get_connection
from database.runtime_schema import ensure_runtime_schema
from database.postgres_config import PostgresSettings

from .memory_importance import MemoryImportanceScorer
from .memory_policy import MemoryPolicy
from .memory_sanitizer import MemorySanitizer
from .memory_types import MemoryRecord, MemoryStatus, MemoryType, is_record_expired


class PostgresMemoryStore:
    backend_name = "postgresql"
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
        self._ensure_schema()

    def _connect(self):
        return get_connection(runtime=True)

    def describe(self) -> dict[str, Any]:
        settings = PostgresSettings.from_env()
        return {
            "backend_name": self.backend_name,
            "storage_kind": "relational",
            "schema": settings.runtime_schema,
            "available": True,
        }

    def _ensure_schema(self) -> None:
        ensure_runtime_schema()

    def upsert(self, record: MemoryRecord | dict[str, Any]) -> MemoryRecord:
        safe = self.sanitizer.sanitize_record(record)
        self.policy.assert_can_store(safe)
        if safe.importance <= 0.0:
            safe.importance = self.importance_scorer.score(safe)
        payload = _record_to_row(safe)
        with self._connect() as conn:
            with conn:
                columns = list(payload.keys())
                placeholders = ", ".join("%s" for _ in columns)
                updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "memory_id")
                conn.execute(
                    f"""
                    INSERT INTO memory_records ({", ".join(columns)})
                    VALUES ({placeholders})
                    ON CONFLICT(memory_id) DO UPDATE SET {updates}
                    """,
                    [payload[column] for column in columns],
                )
        return safe

    def get(self, memory_id: str, *, user_id: str = "", include_deleted: bool = False) -> MemoryRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM memory_records WHERE memory_id = %s", (str(memory_id or ""),)).fetchone()
        if not row:
            return None
        record = _row_to_record(row)
        if user_id and record.user_id != str(user_id):
            return None
        if not include_deleted and record.status == MemoryStatus.DELETED:
            return None
        return record

    def delete(self, memory_id: str, *, user_id: str = "", hard: bool = False) -> bool:
        record = self.get(memory_id, user_id=user_id, include_deleted=True)
        if not record:
            return False
        with self._connect() as conn:
            with conn:
                if hard:
                    conn.execute("DELETE FROM memory_records WHERE memory_id = %s", (record.memory_id,))
                else:
                    conn.execute(
                        "UPDATE memory_records SET status = %s, updated_at = CURRENT_TIMESTAMP::text WHERE memory_id = %s",
                        (MemoryStatus.DELETED.value, record.memory_id),
                    )
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
        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id = %s")
            params.append(str(user_id))
        if status:
            clauses.append("status = %s")
            params.append(MemoryStatus.from_value(status).value)
        allowed_types = [MemoryType.from_value(item).value for item in (memory_types or [])]
        if allowed_types:
            clauses.append(f"memory_type IN ({', '.join('%s' for _ in allowed_types)})")
            params.extend(allowed_types)
        if created_after:
            clauses.append("created_at >= %s")
            params.append(str(created_after))
        if created_before:
            clauses.append("created_at <= %s")
            params.append(str(created_before))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM memory_records {where} ORDER BY updated_at DESC LIMIT %s",
                [*params, max(1, int(limit or 1000)) * 5],
            ).fetchall()
        topic_set = {str(item).lower() for item in (topics or [])}
        stock_set = {str(item).split(".")[0].zfill(6) for item in (stock_codes or [])}
        records: list[MemoryRecord] = []
        for row in rows:
            record = _row_to_record(row)
            if not include_expired and is_record_expired(record):
                continue
            if record.importance < float(min_importance or 0.0):
                continue
            if topic_set and not (topic_set & {item.lower() for item in record.topics}):
                continue
            if stock_set and not (stock_set & set(record.stock_codes)):
                continue
            records.append(record)
            if len(records) >= max(1, int(limit or 100)):
                break
        return records

    def count(
        self,
        *,
        user_id: str = "",
        status: MemoryStatus | str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id = %s")
            params.append(str(user_id))
        if status is not None:
            clauses.append("status = %s")
            params.append(MemoryStatus.from_value(status).value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            return int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM memory_records{where}",
                    params,
                ).fetchone()["count"]
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
        record.metadata = {
            **dict(record.metadata or {}),
            **dict(metadata_updates or {}),
        }
        if clear_expiry:
            record.valid_until = ""
        record.updated_at = datetime.now().isoformat(timespec="seconds")
        return self.upsert(record)


def _json_dumps(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _record_to_row(record: MemoryRecord) -> dict[str, Any]:
    return {
        "memory_id": record.memory_id,
        "user_id": record.user_id,
        "conversation_id": record.conversation_id,
        "run_id": record.run_id,
        "task_id": record.task_id,
        "source_type": record.source_type,
        "source_id": record.source_id,
        "memory_type": record.memory_type.value,
        "memory_subtype": record.memory_subtype,
        "scope": record.scope.value,
        "visibility": record.visibility.value,
        "status": record.status.value,
        "content": record.content,
        "summary": record.summary,
        "topics_json": _json_dumps(record.topics),
        "stock_codes_json": _json_dumps(record.stock_codes),
        "importance": record.importance,
        "confidence": record.confidence,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "valid_from": record.valid_from,
        "valid_until": record.valid_until,
        "supersedes_memory_id": record.supersedes_memory_id,
        "metadata_json": _json_dumps(record.metadata),
        "context_refs_json": _json_dumps(record.context_refs),
        "message_refs_json": _json_dumps(record.message_refs),
        "artifact_refs_json": _json_dumps(record.artifact_refs),
        "approval_refs_json": _json_dumps(record.approval_refs),
        "source_refs_json": _json_dumps(record.source_refs),
    }


def _row_to_record(row: dict[str, Any]) -> MemoryRecord:
    data = dict(row)
    return MemoryRecord(
        memory_id=data.get("memory_id") or "",
        user_id=data.get("user_id") or "",
        conversation_id=data.get("conversation_id") or "",
        run_id=data.get("run_id") or "",
        task_id=data.get("task_id") or "",
        source_type=data.get("source_type") or "",
        source_id=data.get("source_id") or "",
        memory_type=data.get("memory_type") or MemoryType.EPISODIC,
        memory_subtype=data.get("memory_subtype") or "",
        scope=data.get("scope") or "",
        visibility=data.get("visibility") or "",
        status=data.get("status") or "",
        content=data.get("content") or "",
        summary=data.get("summary") or "",
        topics=_json_loads(data.get("topics_json"), []),
        stock_codes=_json_loads(data.get("stock_codes_json"), []),
        importance=data.get("importance") or 0.0,
        confidence=data.get("confidence") or 0.0,
        created_at=data.get("created_at") or "",
        updated_at=data.get("updated_at") or "",
        valid_from=data.get("valid_from") or "",
        valid_until=data.get("valid_until") or "",
        supersedes_memory_id=data.get("supersedes_memory_id") or "",
        metadata=_json_loads(data.get("metadata_json"), {}),
        context_refs=_json_loads(data.get("context_refs_json"), []),
        message_refs=_json_loads(data.get("message_refs_json"), []),
        artifact_refs=_json_loads(data.get("artifact_refs_json"), []),
        approval_refs=_json_loads(data.get("approval_refs_json"), []),
        source_refs=_json_loads(data.get("source_refs_json"), []),
    )
