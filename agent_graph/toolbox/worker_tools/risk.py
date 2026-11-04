"""Atomic read-only tools for the W04 Risk Analyst.

These tools calculate or expose structured facts only. They do not decide the
user-facing risk conclusion, choose a proposal, or mutate business state. W04's
LLM interprets their structured outputs under its prompt and schema.
"""

from __future__ import annotations

from typing import Any

from agent_graph.workers.cards import RISK_ANALYST
from agent_graph.toolbox import (
    OP_READ,
    TOOL_VISIBILITY_WORKER_PRIVATE,
    ToolDefinition,
    ToolInputContract,
    ToolOutputContract,
    description,
    result_schema,
    schema,
)

RISK_CALCULATE_CONCENTRATION = "risk.calculate_concentration"
RISK_READ_ACCOUNT_RISK_FACTS = "risk.read_account_risk_facts"
RISK_SUMMARIZE_EXPOSURE = "risk.summarize_exposure"
RISK_FINALIZE_FACTS = "risk.finalize_facts"


def _unwrap(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    data = value.get("data")
    if isinstance(data, dict):
        return data
    return value


def _positions(value: Any) -> list[dict[str, Any]]:
    root = _unwrap(value)
    for key in ("display_positions", "positions", "holdings"):
        rows = root.get(key)
        if isinstance(rows, list):
            return [dict(item) for item in rows if isinstance(item, dict)]
    nested = root.get("portfolio_state")
    if isinstance(nested, dict):
        return _positions(nested)
    return []


def _number(row: dict[str, Any], *keys: str) -> float:
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _security_ref(row: dict[str, Any]) -> str:
    for key in ("security_ref", "graph_ref", "stock_code", "code", "symbol"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    position_id = str(row.get("position_id") or "").strip()
    tail = position_id.rsplit("_", 1)[-1] if position_id else ""
    if len(tail) == 6 and tail.isdigit():
        return tail
    return position_id


def _root_number(root: dict[str, Any], *keys: str) -> float:
    value = _number(root, *keys)
    if value > 0:
        return value
    for nested_key in (
        "account", "account_summary", "cash_state", "portfolio_totals",
        "account_snapshot", "portfolio_summary",
    ):
        nested = root.get(nested_key)
        if isinstance(nested, dict):
            value = _number(nested, *keys)
            if value > 0:
                return value
    return 0.0


def _user_constraints(root: dict[str, Any]) -> dict[str, Any]:
    value = root.get("user_constraints")
    return dict(value) if isinstance(value, dict) else {}


def build_risk_tool_definitions() -> list[ToolDefinition]:
    def concentration(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        root = _unwrap(arguments.get("portfolio_state"))
        rows = _positions(root)
        values = [max(0.0, _number(row, "market_value", "position_market_value")) for row in rows]
        position_market_value = sum(values)

        invested_weights = [
            value / position_market_value for value in values
        ] if position_market_value > 0 else []
        sorted_invested_weights = sorted(invested_weights, reverse=True)

        total_assets = max(0.0, _root_number(root, "total_assets", "total_asset", "asset_total"))
        asset_weights = [value / total_assets for value in values] if total_assets > 0 else []
        sorted_asset_weights = sorted(asset_weights, reverse=True)

        constraints = _user_constraints(root)
        max_single_position = max(0.0, _number(constraints, "max_single_position"))
        breaches: list[dict[str, Any]] = []
        if total_assets > 0 and max_single_position > 0:
            for index, row in enumerate(rows):
                asset_weight = asset_weights[index] if index < len(asset_weights) else 0.0
                if asset_weight > max_single_position + 1e-12:
                    breaches.append({
                        "security_ref": _security_ref(row),
                        "current_asset_weight": asset_weight,
                        "max_allowed_asset_weight": max_single_position,
                        "excess_asset_weight": asset_weight - max_single_position,
                    })
            breaches.sort(key=lambda item: float(item["current_asset_weight"]), reverse=True)

        limitations: list[str] = []
        if max_single_position > 0 and total_assets <= 0:
            limitations.append("total_assets_missing_for_single_position_constraint")

        metric_basis = {
            "legacy_weight_fields": "position_market_value",
            "invested_weight": "position_market_value",
            "asset_weight": "total_assets",
            "max_single_position_constraint": "total_assets",
        }
        weight_rows = [
            {
                "security_ref": _security_ref(row),
                "weight": invested_weights[index] if index < len(invested_weights) else 0.0,
                "invested_weight": invested_weights[index] if index < len(invested_weights) else 0.0,
                "asset_weight": asset_weights[index] if index < len(asset_weights) else None,
            }
            for index, row in enumerate(rows)
        ]
        facts = {
            "position_count": len(rows),
            "position_market_value": position_market_value,
            "total_assets": total_assets,
            "largest_position_weight": sorted_invested_weights[0] if sorted_invested_weights else 0.0,
            "top3_weight": sum(sorted_invested_weights[:3]),
            "herfindahl_index": sum(weight * weight for weight in invested_weights),
            "largest_position_invested_weight": sorted_invested_weights[0] if sorted_invested_weights else 0.0,
            "top3_invested_weight": sum(sorted_invested_weights[:3]),
            "herfindahl_index_invested": sum(weight * weight for weight in invested_weights),
            "largest_position_asset_weight": sorted_asset_weights[0] if sorted_asset_weights else 0.0,
            "top3_asset_weight": sum(sorted_asset_weights[:3]),
            "invested_asset_ratio": (position_market_value / total_assets if total_assets > 0 else 0.0),
            "metric_basis": metric_basis,
            "weights": weight_rows,
            "max_single_position_limit": max_single_position,
            "single_position_limit_breach_count": len(breaches),
            "single_position_limit_breaches": breaches,
        }
        source_refs = ["upstream_portfolio_state"]
        if constraints:
            source_refs.append("upstream_user_constraints")
        return {
            "success": True,
            "message": "Portfolio concentration facts calculated with explicit asset and invested bases.",
            "data": {
                **facts,
                "business_empty": not bool(rows),
                "risk_fact_fragment": {
                    "fact_type": "concentration",
                    "facts": facts,
                    "source_refs": source_refs,
                    "limitations": limitations,
                    "business_empty": not bool(rows),
                },
            },
            "warnings": limitations,
            "errors": [],
            "sources": [{"source_id": item} for item in source_refs],
        }

    def account_risk(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        root = _unwrap(arguments.get("portfolio_state"))
        account = root.get("account_snapshot") if isinstance(root.get("account_snapshot"), dict) else {}
        totals = root.get("portfolio_totals") if isinstance(root.get("portfolio_totals"), dict) else {}
        summary = root.get("portfolio_summary") if isinstance(root.get("portfolio_summary"), dict) else {}
        merged = {**summary, **totals, **account}
        keys = (
            "cash", "available_cash", "uninvested_cash", "total_assets", "position_market_value_sum",
            "position_market_value", "cash_ratio", "position_ratio", "max_drawdown", "daily_return",
            "total_return", "time_weighted_return",
        )
        facts = {key: merged.get(key) for key in keys if key in merged}
        return {
            "success": True,
            "message": "Account and portfolio risk facts read.",
            "data": {
                "account_risk_facts": facts,
                "as_of_time": str(root.get("as_of_time") or account.get("as_of_time") or ""),
                "business_empty": not bool(root),
                "risk_fact_fragment": {
                    "fact_type": "account",
                    "facts": facts,
                    "source_refs": ["upstream_account_snapshot"],
                    "limitations": [],
                    "business_empty": not bool(root),
                },
            },
            "warnings": [],
            "errors": [],
            "sources": [{"source_id": "upstream_account_snapshot"}],
        }

    def exposure(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        rows = _positions(arguments.get("portfolio_state"))
        by_industry: dict[str, float] = {}
        by_market: dict[str, float] = {}
        unresolved = 0
        for row in rows:
            value = max(0.0, _number(row, "market_value", "position_market_value"))
            industry = str(row.get("industry") or row.get("sector") or "").strip()
            market = str(row.get("exchange") or row.get("market") or "").strip()
            if industry:
                by_industry[industry] = by_industry.get(industry, 0.0) + value
            else:
                unresolved += 1
            if market:
                by_market[market] = by_market.get(market, 0.0) + value
        total = sum(max(0.0, _number(row, "market_value", "position_market_value")) for row in rows)
        normalize = lambda mapping: {
            key: (value / total if total > 0 else 0.0) for key, value in mapping.items()
        }
        return {
            "success": True,
            "message": "Portfolio exposure facts summarized.",
            "data": {
                "industry_exposure": normalize(by_industry),
                "market_exposure": normalize(by_market),
                "unresolved_industry_count": unresolved,
                "position_count": len(rows),
                "business_empty": not bool(rows),
                "risk_fact_fragment": {
                    "fact_type": "exposure",
                    "facts": {
                        "industry_exposure": normalize(by_industry),
                        "market_exposure": normalize(by_market),
                        "unresolved_industry_count": unresolved,
                        "position_count": len(rows),
                    },
                    "source_refs": ["upstream_portfolio_positions"],
                    "limitations": (["industry_metadata_incomplete"] if unresolved else []),
                    "business_empty": not bool(rows),
                },
            },
            "warnings": (["industry_metadata_incomplete"] if unresolved else []),
            "errors": [],
            "sources": [{"source_id": "upstream_portfolio_positions"}],
        }

    def finalize(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        collections = arguments.get("collections") or []
        if not isinstance(collections, list):
            raise ValueError("risk_fact_collections_list_required")
        facts: list[dict[str, Any]] = []
        source_refs: list[str] = []
        limitations: list[str] = []
        successful = 0
        for item in collections:
            if not isinstance(item, dict):
                raise ValueError("risk_fact_fragment_semantic_payload_required")
            successful += 1
            fact_payload = item.get("facts") if isinstance(item.get("facts"), dict) else {}
            facts.append({
                "fact_type": str(item.get("fact_type") or "unknown"),
                **dict(fact_payload),
                "business_empty": bool(item.get("business_empty", False)),
            })
            source_refs.extend(str(src) for src in item.get("source_refs") or [] if str(src))
            limitations.extend(str(w) for w in item.get("limitations") or [] if str(w))
        return {
            "success": successful > 0,
            "message": "Risk facts finalized." if successful else "No risk fact tool completed.",
            "data": {
                "risk_facts": facts,
                "source_refs": list(dict.fromkeys(item for item in source_refs if item)),
                "limitations": list(dict.fromkeys(limitations)),
                "source_tool_count": len(collections),
                "successful_tool_count": successful,
                "business_empty": bool(facts) and all(bool(item.get("business_empty")) for item in facts),
            },
            "warnings": list(dict.fromkeys(limitations)),
            "errors": [] if successful else ["risk_fact_tools_failed"],
            "error_type": "risk_fact_tools_failed" if not successful else "",
            "failure_kind": "tool_failure" if not successful else "",
            "retryable": bool(not successful),
            "sources": [{"source_id": item} for item in list(dict.fromkeys(source_refs)) if item],
        }

    common_input = schema({"portfolio_state": {"type": "object"}}, required=["portfolio_state"])
    common = dict(
        operation_type=OP_READ,
        allowed_agent_types=[RISK_ANALYST],
        permission_scope=OP_READ,
        visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
        side_effects=[],
        mutates_business_state=False,
        audit_level="full",
    )
    return [
        ToolDefinition(
            name=RISK_CALCULATE_CONCENTRATION,
            display_name="Calculate Portfolio Concentration",
            description=description(
                "Calculate concentration metrics with explicit invested-capital and total-asset bases and detect hard single-position constraint breaches.",
                "W04 needs labeled position weights, concentration metrics, or deterministic max_single_position compliance facts.",
                "Business risk judgment, proposal generation, persistence, or execution.",
                "A structured upstream portfolio_state.",
                "Deterministic concentration facts.",
            ),
            input_schema=common_input,
            output_schema=result_schema(["largest_position_weight", "top3_weight", "herfindahl_index", "largest_position_asset_weight", "top3_asset_weight", "single_position_limit_breaches"]),
            execution_handler=concentration,
            produced_outputs=["largest_position_weight", "top3_weight", "herfindahl_index", "largest_position_asset_weight", "top3_asset_weight", "single_position_limit_breaches"],
            input_contracts=[ToolInputContract(slot_id="portfolio_state", required=True)],
            output_contracts=[
                ToolOutputContract(slot_id="largest_position_weight", source_path="data.largest_position_weight"),
                ToolOutputContract(slot_id="top3_weight", source_path="data.top3_weight"),
                ToolOutputContract(slot_id="herfindahl_index", source_path="data.herfindahl_index"),
                ToolOutputContract(
                    slot_id="concentration_risk_fragment",
                    schema_id="RiskFactFragment.v1",
                    source_path="data.risk_fact_fragment",
                ),
            ],
            idempotency="pure_transform",
            tags=["risk", "concentration", "atomic", "read_only"],
            **common,
        ),
        ToolDefinition(
            name=RISK_READ_ACCOUNT_RISK_FACTS,
            display_name="Read Account Risk Facts",
            description=description(
                "Read structured cash, asset, return and drawdown facts already present in the upstream snapshot.",
                "W04 needs account-level risk facts without another business-state query.",
                "Interpretation, proposals, persistence, or execution.",
                "A structured upstream portfolio_state.",
                "Account risk facts and as-of time.",
            ),
            input_schema=common_input,
            output_schema=result_schema(["account_risk_facts", "as_of_time"]),
            execution_handler=account_risk,
            produced_outputs=["account_risk_facts", "as_of_time"],
            input_contracts=[ToolInputContract(slot_id="portfolio_state", required=True)],
            output_contracts=[
                ToolOutputContract(slot_id="account_risk_facts", source_path="data.account_risk_facts"),
                ToolOutputContract(slot_id="as_of_time", source_path="data.as_of_time"),
                ToolOutputContract(
                    slot_id="account_risk_fragment",
                    schema_id="RiskFactFragment.v1",
                    source_path="data.risk_fact_fragment",
                ),
            ],
            idempotency="read_only",
            tags=["risk", "account", "atomic", "read_only"],
            **common,
        ),
        ToolDefinition(
            name=RISK_SUMMARIZE_EXPOSURE,
            display_name="Summarize Portfolio Exposure",
            description=description(
                "Summarize industry and market exposure from structured positions.",
                "W04 needs exposure facts and metadata coverage limitations.",
                "Business judgment, proposals, persistence, or execution.",
                "A structured upstream portfolio_state.",
                "Exposure maps and unresolved metadata counts.",
            ),
            input_schema=common_input,
            output_schema=result_schema(["industry_exposure", "market_exposure", "unresolved_industry_count"]),
            execution_handler=exposure,
            produced_outputs=["industry_exposure", "market_exposure", "unresolved_industry_count"],
            input_contracts=[ToolInputContract(slot_id="portfolio_state", required=True)],
            output_contracts=[
                ToolOutputContract(slot_id="industry_exposure", source_path="data.industry_exposure"),
                ToolOutputContract(slot_id="market_exposure", source_path="data.market_exposure"),
                ToolOutputContract(slot_id="unresolved_industry_count", source_path="data.unresolved_industry_count"),
                ToolOutputContract(
                    slot_id="exposure_risk_fragment",
                    schema_id="RiskFactFragment.v1",
                    source_path="data.risk_fact_fragment",
                ),
            ],
            idempotency="pure_transform",
            tags=["risk", "exposure", "atomic", "read_only"],
            **common,
        ),
        ToolDefinition(
            name=RISK_FINALIZE_FACTS,
            display_name="Finalize Risk Facts",
            description=description(
                "Merge selected atomic risk-tool results into one structured fact bundle for W04 interpretation.",
                "W04 has completed one or more risk fact calculations.",
                "Risk conclusion, recommendation, proposal, persistence, or execution.",
                "collections of normalized Tool results.",
                "risk_facts, source_refs and limitations.",
            ),
            input_schema=schema({"collections": {"type": "array"}}, required=["collections"]),
            output_schema=result_schema(["risk_facts", "source_refs", "limitations"]),
            execution_handler=finalize,
            produced_outputs=["risk_facts", "source_refs", "limitations"],
            input_contracts=[
                ToolInputContract(
                    slot_id="collections",
                    schema_id="RiskFactFragment.v1",
                    required=True,
                    accepted_sources=("upstream_tool",),
                    cardinality="many",
                    description="One or more risk-fact fragments selected by the Tool DAG.",
                )
            ],
            output_contracts=[
                ToolOutputContract(slot_id="risk_facts", source_path="data.risk_facts"),
                ToolOutputContract(slot_id="source_refs", source_path="data.source_refs"),
                ToolOutputContract(slot_id="limitations", source_path="data.limitations"),
            ],
            idempotency="pure_transform",
            tags=["risk", "finalizer", "atomic", "read_only"],
            **common,
        ),
    ]


__all__ = [
    "RISK_CALCULATE_CONCENTRATION",
    "RISK_READ_ACCOUNT_RISK_FACTS",
    "RISK_SUMMARIZE_EXPOSURE",
    "RISK_FINALIZE_FACTS",
    "build_risk_tool_definitions",
]
