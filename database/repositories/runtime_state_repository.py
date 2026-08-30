from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from database.schemas import json_dumps, json_loads
from database.postgres_store import PostgresStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RuntimeStateRepository:
    """Database authority for small live state payloads without a domain table."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.store = PostgresStore()

    def put(
        self,
        state_kind: str,
        payload: dict[str, Any] | list[Any],
        *,
        user_id: str = "",
        scope_id: str = "",
        as_of_date: str = "",
    ) -> dict[str, Any]:
        kind = str(state_kind or "").strip()
        if not kind:
            raise ValueError("runtime_state_kind_required")
        user = str(user_id or "")
        scope = str(scope_id or "")
        now = _now()
        state_id = "state_" + uuid5(
            NAMESPACE_URL,
            f"runtime-state|{kind}|{user}|{scope}",
        ).hex[:24]
        existing = self.store.get("runtime_state_snapshot", {"state_id": state_id})
        return self.store.upsert(
            "runtime_state_snapshot",
            {
                "state_id": state_id,
                "state_kind": kind,
                "user_id": user,
                "scope_id": scope,
                "as_of_date": str(as_of_date or ""),
                "payload_json": json_dumps(payload),
                "created_at": str((existing or {}).get("created_at") or now),
                "updated_at": now,
            },
        )

    def get(
        self,
        state_kind: str,
        *,
        user_id: str = "",
        scope_id: str | None = "",
    ) -> dict[str, Any] | list[Any] | None:
        filters = {
            "state_kind": str(state_kind or ""),
            "user_id": str(user_id or ""),
        }
        if scope_id is not None:
            filters["scope_id"] = str(scope_id or "")
        rows = self.store.list(
            "runtime_state_snapshot",
            filters=filters,
            order_by="updated_at",
            descending=True,
            limit=1,
        )
        if not rows:
            return None
        return json_loads(rows[0].get("payload_json"), default={})
