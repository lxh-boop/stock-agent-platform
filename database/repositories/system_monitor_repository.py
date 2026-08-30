from __future__ import annotations

from pathlib import Path
from typing import Any

from database.schemas import json_dumps, json_loads
from database.postgres_store import PostgresStore


SNAPSHOT_JSON_FIELDS = [
    "data_metrics_json",
    "model_metrics_json",
    "rag_metrics_json",
    "agent_metrics_json",
    "portfolio_metrics_json",
    "version_info_json",
    "missing_modules_json",
]

SNAPSHOT_JSON_ALIASES = {
    "data_metrics": "data_metrics_json",
    "model_metrics": "model_metrics_json",
    "rag_metrics": "rag_metrics_json",
    "agent_metrics": "agent_metrics_json",
    "portfolio_metrics": "portfolio_metrics_json",
    "version_info": "version_info_json",
    "missing_modules": "missing_modules_json",
}


class SystemMonitorRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.store = PostgresStore()

    def _encode_snapshot(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        for alias, target in SNAPSHOT_JSON_ALIASES.items():
            if alias in payload and target not in payload:
                payload[target] = payload.pop(alias)
        for key in SNAPSHOT_JSON_FIELDS:
            if isinstance(payload.get(key), (dict, list)):
                payload[key] = json_dumps(payload[key])
        return payload

    def _decode_snapshot(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        for alias, target in SNAPSHOT_JSON_ALIASES.items():
            payload[alias] = json_loads(payload.get(target), default={} if alias != "missing_modules" else [])
        return payload

    def upsert_snapshot(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("system_monitor_snapshots", self._encode_snapshot(record))

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._decode_snapshot(self.store.get("system_monitor_snapshots", {"snapshot_id": snapshot_id}))

    def list_snapshots(self, user_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        filters = {"user_id": user_id} if user_id else None
        rows = self.store.list(
            "system_monitor_snapshots",
            filters=filters,
            order_by="updated_at",
        )
        if limit is not None:
            rows = rows[-int(limit):]
        rows = list(reversed(rows))
        return [self._decode_snapshot(row) or {} for row in rows]

    def upsert_alert(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("system_monitor_alerts", dict(record))

    def list_alerts(
        self,
        snapshot_id: str | None = None,
        user_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = {}
        if snapshot_id:
            filters["snapshot_id"] = snapshot_id
        if user_id:
            filters["user_id"] = user_id
        rows = self.store.list(
            "system_monitor_alerts",
            filters=filters or None,
            order_by="updated_at",
        )
        if limit is not None:
            rows = rows[-int(limit):]
        return list(reversed(rows))
