from __future__ import annotations

from pathlib import Path
from typing import Any

from database.schemas import json_dumps, json_loads
from database.postgres_store import PostgresStore


class UserRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.store = PostgresStore()

    def insert_user_profile(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        if "trading_permissions" in payload:
            payload["trading_permissions_json"] = json_dumps(
                payload.pop("trading_permissions")
            )
        saved = self.store.upsert("user_profile", payload)
        saved["trading_permissions"] = json_loads(
            saved.pop("trading_permissions_json", "{}"),
            default={},
        )
        return saved

    def get_user_profile(self, user_id: str) -> dict[str, Any] | None:
        row = self.store.get("user_profile", {"user_id": user_id})
        if row:
            row["trading_permissions"] = json_loads(
                row.pop("trading_permissions_json", "{}"),
                default={},
            )
        return row

    def list_user_profiles(self, limit: int | None = None) -> list[dict[str, Any]]:
        return self.store.list("user_profile", order_by="user_id", limit=limit)

    def update_user_profile(self, user_id: str, changes: dict[str, Any]) -> int:
        return self.store.update("user_profile", {"user_id": user_id}, changes)

    def insert_risk_assessment(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("risk_assessment", record)

    def get_risk_assessment(self, assessment_id: str) -> dict[str, Any] | None:
        return self.store.get("risk_assessment", {"assessment_id": assessment_id})

    def list_risk_assessments(self, user_id: str | None = None) -> list[dict[str, Any]]:
        filters = {"user_id": user_id} if user_id else None
        return self.store.list("risk_assessment", filters=filters, order_by="assessment_time")

    def update_risk_assessment(self, assessment_id: str, changes: dict[str, Any]) -> int:
        return self.store.update("risk_assessment", {"assessment_id": assessment_id}, changes)

    def insert_investment_goal(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("investment_goal", record)

    def get_investment_goal(self, goal_id: str) -> dict[str, Any] | None:
        return self.store.get("investment_goal", {"goal_id": goal_id})

    def list_investment_goals(self, user_id: str | None = None) -> list[dict[str, Any]]:
        filters = {"user_id": user_id} if user_id else None
        return self.store.list("investment_goal", filters=filters, order_by="created_at")

    def insert_trading_behavior(self, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert("trading_behavior", record)

    def get_trading_behavior(self, behavior_id: str) -> dict[str, Any] | None:
        return self.store.get("trading_behavior", {"behavior_id": behavior_id})

    def list_trading_behaviors(self, user_id: str | None = None) -> list[dict[str, Any]]:
        filters = {"user_id": user_id} if user_id else None
        return self.store.list("trading_behavior", filters=filters, order_by="updated_at")

    def update_trading_behavior(self, behavior_id: str, changes: dict[str, Any]) -> int:
        return self.store.update("trading_behavior", {"behavior_id": behavior_id}, changes)
