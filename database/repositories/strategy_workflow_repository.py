from __future__ import annotations

from pathlib import Path
from typing import Any

from database.schemas import json_dumps, json_loads
from database.postgres_store import PostgresStore


ACTIVE_PROPOSAL_STATUSES = {
    "draft",
    "revising",
    "locked_for_implementation",
    "implementation_ready",
}


class StrategyWorkflowRepository:
    """PostgreSQL authority for strategy conversation proposals and versions."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.store = PostgresStore()

    @staticmethod
    def _decode_version(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        payload["version"] = int(payload.get("version") or 0)
        payload["proposal_json"] = json_loads(
            payload.get("proposal_json"),
            default={},
        )
        return payload

    def create_proposal(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.insert("strategy_proposals", dict(record))

    def update_proposal(
        self,
        proposal_id: str,
        changes: dict[str, Any],
    ) -> int:
        return self.store.update(
            "strategy_proposals",
            {"proposal_id": proposal_id},
            dict(changes),
        )

    def get_proposal(
        self,
        proposal_id: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        filters: dict[str, Any] = {"proposal_id": proposal_id}
        if user_id:
            filters["user_id"] = user_id
        return self.store.get("strategy_proposals", filters)

    def list_proposals(
        self,
        *,
        user_id: str,
        account_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {"user_id": user_id}
        if account_id is not None:
            filters["account_id"] = account_id
        if conversation_id is not None:
            filters["conversation_id"] = conversation_id
        return self.store.list(
            "strategy_proposals",
            filters=filters,
            order_by="updated_at",
            descending=True,
            limit=limit,
        )

    def get_active_proposal(
        self,
        *,
        user_id: str,
        account_id: str,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        rows = self.list_proposals(
            user_id=user_id,
            account_id=account_id,
            conversation_id=conversation_id,
        )
        return next(
            (
                row
                for row in rows
                if str(row.get("status") or "") in ACTIVE_PROPOSAL_STATUSES
            ),
            None,
        )

    def insert_version(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        payload["proposal_json"] = json_dumps(
            payload.get("proposal_json") or {}
        )
        return self.store.insert("strategy_proposal_versions", payload)

    def get_version(
        self,
        proposal_id: str,
        version: int,
    ) -> dict[str, Any] | None:
        return self._decode_version(
            self.store.get(
                "strategy_proposal_versions",
                {
                    "proposal_id": proposal_id,
                    "version": int(version),
                },
            )
        )

    def list_versions(
        self,
        proposal_id: str,
    ) -> list[dict[str, Any]]:
        return [
            self._decode_version(row) or {}
            for row in self.store.list(
                "strategy_proposal_versions",
                filters={"proposal_id": proposal_id},
                order_by="version",
            )
        ]

    def upsert_implementation(
        self,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        return self.store.upsert(
            "strategy_implementations",
            dict(record),
        )

    def get_implementation(
        self,
        implementation_id: str,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        filters: dict[str, Any] = {
            "implementation_id": implementation_id,
        }
        if user_id:
            filters["user_id"] = user_id
        return self.store.get("strategy_implementations", filters)

    def update_implementation(
        self,
        implementation_id: str,
        changes: dict[str, Any],
    ) -> int:
        return self.store.update(
            "strategy_implementations",
            {"implementation_id": implementation_id},
            dict(changes),
        )

    def get_proposal_implementation(
        self,
        proposal_id: str,
        proposal_version: int,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        filters: dict[str, Any] = {
            "proposal_id": proposal_id,
            "proposal_version": int(proposal_version),
        }
        if user_id:
            filters["user_id"] = user_id
        return self.store.get("strategy_implementations", filters)

    def list_implementations(
        self,
        *,
        user_id: str,
        account_id: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {"user_id": user_id}
        if account_id is not None:
            filters["account_id"] = account_id
        if conversation_id is not None:
            filters["conversation_id"] = conversation_id
        return self.store.list(
            "strategy_implementations",
            filters=filters,
            order_by="updated_at",
            descending=True,
        )
