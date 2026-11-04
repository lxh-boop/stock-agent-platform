from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .contracts import (
    GraphAssertionRecord,
    GraphAuthority,
    GraphObjectRecord,
    GraphPatch,
    GraphRef,
    GraphTermRecord,
)
from .identity import GraphEntityIdentityService
from .patch_validator import GraphPatchValidator


def _safe_time(value: Any) -> str:
    return str(value or "").strip().replace(" ", "T")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class PortfolioGraphService:
    identity: GraphEntityIdentityService
    validator: GraphPatchValidator

    def upsert_snapshot(
        self,
        *,
        user_id: str,
        portfolio_payload: dict[str, Any],
        as_of_time: str,
        source_task_id: str,
        source_agent_id: str,
    ) -> tuple[GraphRef, dict[str, Any]]:
        data = portfolio_payload.get("data") if isinstance(portfolio_payload.get("data"), dict) else portfolio_payload
        positions = list(data.get("positions") or data.get("holdings") or portfolio_payload.get("positions") or [])
        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        effective_time = _safe_time(
            as_of_time
            or data.get("as_of_time")
            or data.get("as_of_date")
            or account.get("updated_at")
            or account.get("trade_date")
        )
        fingerprint = hashlib.sha256(
            f"{user_id}|{effective_time}|{len(positions)}|{account.get('total_assets')}".encode("utf-8")
        ).hexdigest()[:20]
        snapshot_id = f"portfolio_snapshot:{user_id}:{fingerprint}"
        snapshot = GraphObjectRecord(
            object_id=snapshot_id,
            name=f"Portfolio snapshot for {user_id}",
            object_class="portfolio_snapshot",
            properties={
                "user_id": user_id,
                "as_of_time": effective_time,
                "cash": _number(account.get("cash") or data.get("cash")),
                "total_assets": _number(account.get("total_assets") or data.get("total_assets")),
                "market_value": _number(account.get("market_value") or data.get("market_value")),
                "position_count": len(positions),
            },
            authority=GraphAuthority.CANONICAL,
            confidence=1.0,
            source_refs=["portfolio_repository"],
            observed_at=effective_time,
        )
        terms = [GraphTermRecord("predicate:HOLDS", "HOLDS", "predicate", source_refs=["system_kernel"])]
        assertions: list[GraphAssertionRecord] = []
        unresolved: list[dict[str, Any]] = []
        holding_refs: list[GraphRef] = []
        for index, position in enumerate(positions, start=1):
            if not isinstance(position, dict):
                continue
            candidate = str(
                position.get("exchange_symbol")
                or position.get("ts_code")
                or position.get("stock_code")
                or position.get("code")
                or position.get("symbol")
                or position.get("stock_name")
                or position.get("name")
                or ""
            )
            if not candidate:
                unresolved.append({"index": index, "reason": "position_identity_missing"})
                continue
            resolution = self.identity.resolve_request(
                candidate,
                explicit_mentions=[candidate],
                role="holding",
                as_of_time=effective_time,
            )
            if len(resolution.refs) != 1:
                unresolved.append(
                    {
                        "index": index,
                        "candidate": candidate,
                        "ambiguous": resolution.ambiguous_mentions,
                        "unresolved": resolution.unresolved_mentions,
                    }
                )
                continue
            holding_ref = GraphRef(
                **{
                    **resolution.refs[0].to_dict(),
                    "role": "holding",
                    "locked": True,
                    "source": "portfolio_repository",
                }
            )
            holding_refs.append(holding_ref)
            quantity = _number(position.get("quantity") or position.get("shares") or position.get("volume"))
            weight = _number(position.get("weight") or position.get("position_weight"))
            market_value = _number(position.get("market_value") or position.get("position_value"))
            assertions.append(
                GraphAssertionRecord(
                    assertion_id=f"assertion:holds:{snapshot_id}:{holding_ref.node_id}",
                    subject_id=snapshot_id,
                    predicate_term_id="predicate:HOLDS",
                    object_id=holding_ref.node_id,
                    assertion_class="canonical_fact",
                    authority=GraphAuthority.CANONICAL,
                    confidence=1.0,
                    properties={
                        "quantity": quantity,
                        "weight": weight,
                        "market_value": market_value,
                        "cost_price": _number(position.get("cost_price") or position.get("avg_cost")),
                        "current_price": _number(position.get("current_price") or position.get("price")),
                        "source_position": {
                            key: value
                            for key, value in position.items()
                            if key not in {"stock_code", "ts_code", "code", "symbol"}
                        },
                    },
                    source_refs=["portfolio_repository"],
                    valid_from=effective_time,
                    observed_at=effective_time,
                )
            )
        patch = GraphPatch(
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
            source_refs=["portfolio_repository"],
            objects=[snapshot],
            terms=terms,
            assertions=assertions,
        )
        applied = self.validator.validate_and_apply(patch)
        ref = GraphRef(
            graph_id=self.identity.store.graph_id,
            node_id=snapshot_id,
            node_kind="object",
            role="portfolio_snapshot",
            snapshot_id=snapshot_id,
            as_of_time=effective_time,
            source="portfolio_repository",
            confidence=1.0,
            locked=True,
        )
        return ref, {
            "applied": applied,
            "holding_refs": [item.to_dict() for item in holding_refs],
            "unresolved_positions": unresolved,
            "portfolio_payload": portfolio_payload,
        }
