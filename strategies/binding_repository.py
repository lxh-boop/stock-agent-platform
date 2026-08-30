from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from database.postgres_store import PostgresStore


def _now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class StrategyBinding:
    binding_id: str
    user_id: str
    account_id: str
    strategy_id: str
    strategy_version: str
    config_hash: str
    effective_from: str
    status: str
    previous_binding_id: str = ""
    source_plan_id: str = ""
    created_at: str = ""
    activated_at: str = ""
    disabled_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StrategyBinding":
        return cls(
            binding_id=str(value.get("binding_id") or ""),
            user_id=str(value.get("user_id") or ""),
            account_id=str(value.get("account_id") or ""),
            strategy_id=str(value.get("strategy_id") or ""),
            strategy_version=str(value.get("strategy_version") or ""),
            config_hash=str(value.get("config_hash") or ""),
            effective_from=str(value.get("effective_from") or ""),
            status=str(value.get("status") or ""),
            previous_binding_id=str(value.get("previous_binding_id") or ""),
            source_plan_id=str(value.get("source_plan_id") or ""),
            created_at=str(value.get("created_at") or ""),
            activated_at=str(value.get("activated_at") or ""),
            disabled_at=str(value.get("disabled_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyBindingRepository:
    """PostgreSQL-only strategy binding repository."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        _ = db_path
        self.store = PostgresStore()

    def get(
        self,
        binding_id: str,
        *,
        user_id: str | None = None,
    ) -> StrategyBinding | None:
        filters: dict[str, Any] = {"binding_id": binding_id}
        if user_id:
            filters["user_id"] = user_id
        row = self.store.get("strategy_bindings", filters)
        return StrategyBinding.from_dict(row) if row else None

    def list_history(
        self,
        *,
        user_id: str,
        account_id: str,
    ) -> list[StrategyBinding]:
        return [
            StrategyBinding.from_dict(row)
            for row in self.store.list(
                "strategy_bindings",
                filters={"user_id": user_id, "account_id": account_id},
                order_by="created_at",
            )
        ]

    def get_effective(
        self,
        *,
        user_id: str,
        account_id: str,
        as_of_date: str | date | None = None,
    ) -> StrategyBinding | None:
        effective = (
            as_of_date.isoformat()
            if isinstance(as_of_date, date)
            else str(as_of_date or date.today().isoformat())
        )
        candidates = [
            item
            for item in self.list_history(user_id=user_id, account_id=account_id)
            if item.status in {"active", "scheduled"}
            and item.effective_from <= effective
        ]
        candidates.sort(
            key=lambda item: (item.effective_from, item.created_at),
            reverse=True,
        )
        return candidates[0] if candidates else None

    def get_declared_active(
        self,
        *,
        user_id: str,
        account_id: str,
    ) -> StrategyBinding | None:
        rows = self.store.list(
            "strategy_bindings",
            filters={"user_id": user_id, "account_id": account_id, "status": "active"},
            order_by="created_at",
            descending=True,
            limit=1,
        )
        return StrategyBinding.from_dict(rows[0]) if rows else None

    def activate(
        self,
        *,
        user_id: str,
        account_id: str,
        strategy_id: str,
        strategy_version: str,
        config_hash: str,
        effective_from: str,
        source_plan_id: str,
    ) -> StrategyBinding:
        now = _now_text()
        today = datetime.now(UTC).date().isoformat()
        binding_id = f"binding_{uuid4().hex[:20]}"

        with self.store.transaction() as connection:
            current = connection.execute(
                """
                SELECT * FROM strategy_bindings
                WHERE user_id=%s AND account_id=%s AND status='active'
                ORDER BY created_at DESC NULLS LAST, binding_id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (user_id, account_id),
            ).fetchone()

            connection.execute(
                """
                UPDATE strategy_bindings
                SET status='superseded', disabled_at=%s
                WHERE user_id=%s AND account_id=%s AND status='scheduled'
                """,
                (now, user_id, account_id),
            )

            status = "active" if effective_from <= today else "scheduled"
            previous_id = str(current["binding_id"]) if current else ""

            if status == "active" and current:
                connection.execute(
                    """
                    UPDATE strategy_bindings
                    SET status='replaced', disabled_at=%s
                    WHERE binding_id=%s
                    """,
                    (now, previous_id),
                )

            connection.execute(
                """
                INSERT INTO strategy_bindings(
                    binding_id,user_id,account_id,strategy_id,strategy_version,
                    config_hash,effective_from,status,previous_binding_id,
                    source_plan_id,created_at,activated_at,disabled_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    binding_id,user_id,account_id,strategy_id,strategy_version,
                    config_hash,effective_from,status,previous_id or None,
                    source_plan_id,now,now if status == "active" else "","",
                ),
            )
            row = self.store.get(
                "strategy_bindings",
                {"binding_id": binding_id, "user_id": user_id},
                connection=connection,
            )
        if row is None:
            raise RuntimeError("binding_commit_lost")
        return StrategyBinding.from_dict(row)
