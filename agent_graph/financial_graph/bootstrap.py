from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .contracts import (
    GraphAssertionRecord,
    GraphAuthority,
    GraphIdentityRecord,
    GraphObjectRecord,
    GraphPatch,
    GraphTermRecord,
)
from .patch_validator import GraphPatchValidator
from .settings import Neo4jSettings
from .store import Neo4jFinancialGraphStore


def _clean_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text.split(".")[0].zfill(6) if text.split(".")[0].isdigit() else text


def _exchange(value: Any, code: str) -> str:
    text = str(value or "").strip().upper()
    if text in {"SH", "SSE", "XSHG"}:
        return "SSE"
    if text in {"SZ", "SZSE", "XSHE"}:
        return "SZSE"
    if text in {"BJ", "BSE", "XBSE"}:
        return "BSE"
    if code.startswith(("6", "9")):
        return "SSE"
    if code.startswith(("0", "3")):
        return "SZSE"
    if code.startswith(("4", "8")):
        return "BSE"
    return "UNKNOWN"


@dataclass
class FinancialGraphBootstrapper:
    store: Neo4jFinancialGraphStore
    validator: GraphPatchValidator

    @classmethod
    def from_env(cls) -> "FinancialGraphBootstrapper":
        store = Neo4jFinancialGraphStore(Neo4jSettings.from_env())
        return cls(store=store, validator=GraphPatchValidator(store))

    def initialize(self) -> None:
        self.store.verify_connectivity()
        self.store.ensure_schema()
        kernel_terms = [
            GraphTermRecord("predicate:INSTANCE_OF", "INSTANCE_OF", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:ISSUES", "ISSUES", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:BELONGS_TO", "BELONGS_TO", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:HOLDS", "HOLDS", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:INDIRECTLY_HOLDS", "INDIRECTLY_HOLDS", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:TARGETS", "TARGETS", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:AFFECTS", "AFFECTS", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:MENTIONS", "MENTIONS", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:DESCRIBES", "DESCRIBES", "predicate", source_refs=["system_kernel"]),
            GraphTermRecord("predicate:RELATED_TO", "RELATED_TO", "predicate", source_refs=["system_kernel"]),
        ]
        self.validator.validate_and_apply(
            GraphPatch(
                source_task_id="bootstrap",
                source_agent_id="SYSTEM",
                source_refs=["system_kernel"],
                terms=kernel_terms,
            )
        )

    def import_security_master(self, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
        objects: list[GraphObjectRecord] = []
        identities: list[GraphIdentityRecord] = []
        terms = [
            GraphTermRecord("type:tradable_instrument", "Tradable Instrument", "object_type", source_refs=["security_master"]),
            GraphTermRecord("predicate:INSTANCE_OF", "INSTANCE_OF", "predicate", source_refs=["system_kernel"]),
        ]
        assertions: list[GraphAssertionRecord] = []
        for index, row in enumerate(rows, start=1):
            code = _clean_code(row.get("symbol") or row.get("code") or row.get("stock_code") or row.get("ts_code"))
            if not code:
                continue
            exchange = _exchange(row.get("exchange") or row.get("market"), code)
            object_id = f"cn:security:{exchange.lower()}:{code}"
            name = str(row.get("name") or row.get("stock_name") or row.get("display_name") or code)
            aliases = [str(item) for item in row.get("aliases") or [] if str(item).strip()]
            aliases.extend([code, f"{code}.{ {'SSE':'SH','SZSE':'SZ','BSE':'BJ'}.get(exchange, exchange) }"])
            objects.append(
                GraphObjectRecord(
                    object_id=object_id,
                    name=name,
                    object_class="tradable_instrument",
                    aliases=aliases,
                    properties={
                        "market": "CN_A",
                        "exchange": exchange,
                        "currency": str(row.get("currency") or "CNY"),
                        "status": str(row.get("status") or "active"),
                    },
                    authority=GraphAuthority.CANONICAL,
                    confidence=1.0,
                    source_refs=["security_master"],
                )
            )
            values = {
                "symbol": code,
                "exchange_symbol": f"{code}.{ {'SSE':'SH','SZSE':'SZ','BSE':'BJ'}.get(exchange, exchange) }",
                "display_name": name,
            }
            if row.get("ts_code"):
                values["tushare"] = str(row["ts_code"])
            for namespace, value in values.items():
                identities.append(
                    GraphIdentityRecord(
                        identity_id=f"identity:{namespace}:{str(value).upper()}",
                        namespace=namespace,
                        value=str(value),
                        object_id=object_id,
                        identity_type="alias" if namespace == "display_name" else "identifier",
                        is_primary=namespace == "exchange_symbol",
                        source_refs=["security_master"],
                    )
                )
            assertions.append(
                GraphAssertionRecord(
                    assertion_id=f"assertion:instance_of:{object_id}",
                    subject_id=object_id,
                    predicate_term_id="predicate:INSTANCE_OF",
                    literal_value="type:tradable_instrument",
                    assertion_class="canonical_fact",
                    authority=GraphAuthority.CANONICAL,
                    confidence=1.0,
                    source_refs=["security_master"],
                )
            )
        patch = GraphPatch(
            source_task_id="security_master_import",
            source_agent_id="SYSTEM",
            source_refs=["security_master"],
            objects=objects,
            identities=identities,
            terms=terms,
            assertions=assertions,
        )
        return self.validator.validate_and_apply(patch)
