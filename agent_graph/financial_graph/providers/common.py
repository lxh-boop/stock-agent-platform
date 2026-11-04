"""Shared normalization and provider-identity helpers.

This module extracts records and sources from existing service payloads and
resolves provider-private symbols from authoritative GraphRefs. It performs no
business queries, graph writes, or Agent planning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_graph.financial_graph.contracts import GraphNodeKind, GraphRef
from agent_graph.financial_graph.identity import GraphEntityIdentityService


def records_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized record dictionaries from an existing service payload."""

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = data.get("records") or payload.get("records") or data.get("events") or data.get("chunks") or []
    return [dict(item) for item in rows if isinstance(item, dict)]


def sources_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized source dictionaries from an existing service payload."""

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = data.get("sources") or payload.get("sources") or []
    return [dict(item) for item in rows if isinstance(item, dict)]


@dataclass(frozen=True)
class ProviderIdentityResolver:
    """Resolve provider-private identifiers without exposing them to Agents."""

    identity: GraphEntityIdentityService

    def provider_symbol(self, ref: GraphRef) -> str:
        if ref.node_kind != GraphNodeKind.OBJECT:
            raise ValueError("provider_symbol_requires_object_ref")
        value = self.identity.get_identity_value(
            ref,
            namespaces=["symbol", "exchange_symbol", "tushare", "local_symbol"],
        )
        if not value:
            raise RuntimeError(f"provider_identifier_missing:{ref.node_id}")
        return value.split(".")[0]
