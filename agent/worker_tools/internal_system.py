"""Private read-only tools for W02 internal system data queries."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pandas as pd

from agent.collaboration.worker_directory import PORTFOLIO_ANALYST
from agent.graph.provider_adapter import GraphProviderAdapter
from agent.mcp.worker_adapter import invoke_worker_mcp
from agent.tool_runtime import (
    OP_READ,
    TOOL_VISIBILITY_WORKER_PRIVATE,
    ToolDefinition,
    ToolInputContract,
    ToolOutputContract,
    description,
    result_schema,
    schema,
)
from application.web_read_service import web_read_service
from core.config.paths import BACKTEST_MASTER_TABLE_PATH


INTERNAL_PREDICTION_GET_STOCK = "internal.prediction.get_stock"
INTERNAL_RANKING_GET_LATEST = "internal.ranking.get_latest"
INTERNAL_ENTITY_RESOLVE_RANKED_SECURITY = "internal.entity.resolve_ranked_security"
INTERNAL_MODEL_GET_METRICS = "internal.model.get_metrics"
INTERNAL_BACKTEST_GET_SUMMARY = "internal.backtest.get_summary"
INTERNAL_STRATEGY_GET_SELECTED = "internal.strategy.get_selected"
INTERNAL_PORTFOLIO_GET_STATE = "internal.portfolio.get_state"
INTERNAL_ACCOUNT_GET_STATE = "internal.account.get_state"
INTERNAL_USER_PROFILE_GET = "internal.user_profile.get"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.DataFrame):
        return [_jsonable(row) for row in value.to_dict(orient="records")]
    if isinstance(value, pd.Series):
        return _jsonable(value.to_dict())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)



def _prediction_record(row: dict[str, Any]) -> dict[str, Any]:
    result = _jsonable(dict(row or {}))
    if not isinstance(result, dict):
        return {}
    for key in (
        "pred_5d_ret", "up_prob", "score", "confidence", "ret_5",
        "ret_20", "vol_20", "drawdown_20", "risk_score", "pred_score",
    ):
        if key not in result or result[key] in (None, ""):
            continue
        try:
            result[key] = float(result[key])
        except (TypeError, ValueError):
            pass
    for key in ("rank", "pred_rank"):
        if key not in result or result[key] in (None, ""):
            continue
        try:
            result[key] = int(float(result[key]))
        except (TypeError, ValueError):
            pass
    return result

def _stock_code(arguments: dict[str, Any]) -> str:
    text = str(arguments.get("security_node_id") or "")
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if not match:
        raise ValueError("authoritative_security_graph_ref_required")
    return match.group(1)


def _read_backtest_frame() -> pd.DataFrame:
    path = Path(BACKTEST_MASTER_TABLE_PATH)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path)


def build_internal_system_tool_definitions(
    provider: GraphProviderAdapter,
) -> list[ToolDefinition]:
    """Bind W02 private tools to current project services."""

    def get_prediction(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        code = _stock_code(arguments)
        top_k = max(1, min(int(arguments.get("top_k") or 10), 500))
        result = invoke_worker_mcp(
            "model",
            "predict_stock_score",
            {"stock_code": code},
            context,
            caller_tool_id=INTERNAL_PREDICTION_GET_STOCK,
        )
        data = dict(result.get("data") or {})
        records = [dict(row) for row in data.get("records") or [] if isinstance(row, dict)]
        total_count = int(data.get("record_count") or len(records))
        if not result.get("success"):
            return {
                "success": False,
                "message": str(result.get("message") or "Latest ranking is unavailable."),
                "data": {
                    "found": False,
                    "record": {},
                    "data_date": str(data.get("as_of_date") or ""),
                    "rank": None,
                    "is_topk": False,
                    "total_count": total_count,
                    "source_id": "ranking_latest",
                    "reason": "ranking_data_unavailable",
                },
                "warnings": [],
                "errors": ["ranking_data_unavailable"],
                "sources": [{"source_id": "ranking_latest"}],
            }
        record = records[0] if records else {}
        raw_rank = record.get("rank", record.get("pred_rank"))
        try:
            rank = int(float(raw_rank)) if raw_rank not in (None, "") else None
        except (TypeError, ValueError):
            rank = None
        return {
            "success": True,
            "message": "Stock prediction queried." if record else "Stock is not present in the latest prediction universe.",
            "data": {
                "found": bool(record),
                "record": _prediction_record(record),
                "data_date": str(data.get("as_of_date") or ""),
                "rank": rank,
                "is_topk": bool(rank is not None and rank <= top_k),
                "total_count": total_count,
                "security_node_id": str(arguments.get("security_node_id") or ""),
                "selected_entity_ref": _jsonable(arguments.get("selected_entity_ref") or {}),
                "source_id": "ranking_latest",
                "reason": "" if record else "stock_not_in_prediction_universe",
            },
            "warnings": [],
            "errors": [],
            "sources": [{"source_id": "ranking_latest", "as_of_date": str(data.get("as_of_date") or "")}],
        }

    def get_ranking(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        top_k = max(1, min(int(arguments.get("top_k") or 10), 500))
        result = invoke_worker_mcp(
            "model",
            "predict_rank",
            {"top_k": top_k},
            context,
            caller_tool_id=INTERNAL_RANKING_GET_LATEST,
        )
        data = dict(result.get("data") or {})
        return {
            "success": bool(result.get("success")),
            "message": str(result.get("message") or ""),
            "data": {
                "records": _jsonable(data.get("records") or []),
                "summary": {"returned_count": int(data.get("record_count") or 0)},
                "data_date": str(data.get("as_of_date") or ""),
                "source_id": "ranking_latest",
            },
            "warnings": [],
            "errors": [] if result.get("success") else [str(result.get("status") or "ranking_data_unavailable")],
            "sources": [{"source_id": "ranking_latest"}],
        }

    def resolve_ranked_security(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        """Resolve a security discovered by an upstream ranking Tool.

        Identity is derived only from structured internal ranking records and is
        verified by Neo4j.  The Worker planner may therefore discover an entity
        during execution without guessing it from free text.
        """

        raw = arguments.get("market_ranking_signals")
        if isinstance(raw, list):
            records = [dict(item) for item in raw if isinstance(item, dict)]
            data_date = str(arguments.get("as_of_time") or "")
        else:
            payload = dict(raw or {}) if isinstance(raw, dict) else {}
            nested = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            records = [dict(item) for item in nested.get("records") or [] if isinstance(item, dict)]
            data_date = str(nested.get("data_date") or arguments.get("as_of_time") or "")

        if not records:
            return {
                "success": False,
                "message": "Ranking result contains no security candidate.",
                "data": {
                    "security_node_id": "",
                    "selected_entity_ref": {},
                    "selected_ranking_record": {},
                    "business_empty": True,
                },
                "warnings": [],
                "errors": ["ranking_security_candidate_missing"],
                "error_type": "business_empty",
                "failure_kind": "business_empty",
                "retryable": False,
                "sources": [{"source_id": "ranking_latest", "as_of_date": data_date}],
            }

        # Ranking rows are already structured internal data. Prefer explicit
        # security identity fields; never mine arbitrary numbers from narrative.
        selected = records[0]
        identity_values: list[str] = []
        for key in ("exchange_symbol", "ts_code", "symbol", "stock_code", "code", "instrument"):
            value = str(selected.get(key) or "").strip()
            if value and value not in identity_values:
                identity_values.append(value)
        if not identity_values:
            return {
                "success": False,
                "message": "Top ranking row has no declared security identity.",
                "data": {
                    "security_node_id": "",
                    "selected_entity_ref": {},
                    "selected_ranking_record": _prediction_record(selected),
                },
                "warnings": [],
                "errors": ["ranking_security_identity_missing"],
                "error_type": "entity_resolution_failure",
                "failure_kind": "entity_resolution_failure",
                "retryable": False,
                "sources": [{"source_id": "ranking_latest", "as_of_date": data_date}],
            }

        candidates = []
        seen_node_ids: set[str] = set()
        for value in identity_values:
            for candidate in provider.identity.resolve_identity(
                value,
                role="focus",
                locked=True,
                as_of_time=data_date,
            ):
                node_id = str(candidate.graph_ref.node_id or "")
                if node_id and node_id not in seen_node_ids:
                    candidates.append(candidate)
                    seen_node_ids.add(node_id)

        if len(candidates) != 1:
            return {
                "success": False,
                "message": (
                    "Top ranking security cannot be resolved uniquely in the authoritative graph."
                ),
                "data": {
                    "security_node_id": "",
                    "selected_entity_ref": {},
                    "selected_ranking_record": _prediction_record(selected),
                    "candidate_count": len(candidates),
                },
                "warnings": [],
                "errors": ["ranking_security_graph_ref_unresolved"],
                "error_type": "entity_resolution_failure",
                "failure_kind": "entity_resolution_failure",
                "retryable": False,
                "sources": [{"source_id": "neo4j_identity", "as_of_date": data_date}],
            }

        candidate = candidates[0]
        ref = candidate.graph_ref
        return {
            "success": True,
            "message": "Ranked security resolved to authoritative GraphRef.",
            "data": {
                "security_node_id": ref.node_id,
                "selected_entity_ref": ref.to_dict(),
                "selected_entity": provider.public_entity_descriptor(ref),
                "selected_ranking_record": _prediction_record(selected),
                "data_date": data_date,
                "source_id": "neo4j_identity",
            },
            "warnings": [],
            "errors": [],
            "sources": [{"source_id": "neo4j_identity", "as_of_date": data_date}],
        }

    def get_metrics(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        value = _jsonable(web_read_service.metrics())
        model_name = str(arguments.get("model_name") or "").strip().lower()
        if model_name and isinstance(value, list):
            selected = [row for row in value if model_name in str(row.get("model_name") or row.get("model") or "").lower()]
            if selected:
                value = selected
        found = value not in (None, {}, [])
        return {
            "success": found,
            "message": "Model metrics queried." if found else "Model metrics are unavailable.",
            "data": {"metrics": value if found else {}, "model_name": str(arguments.get("model_name") or ""), "source_id": "model_metrics"},
            "warnings": [],
            "errors": [] if found else ["model_metrics_unavailable"],
            "sources": [{"source_id": "model_metrics"}],
        }

    def get_backtest(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        frame = _read_backtest_frame()
        if frame.empty:
            return {
                "success": False,
                "message": "Backtest summary is unavailable.",
                "data": {"records": [], "summary": {}, "source_id": "backtest_master_table"},
                "warnings": [],
                "errors": ["backtest_data_unavailable"],
                "sources": [{"source_id": "backtest_master_table"}],
            }
        data = frame.copy()
        model_name = str(arguments.get("model_name") or "").strip().lower()
        if model_name:
            for column in ("model_name", "model", "backend"):
                if column in data.columns:
                    matched = data[data[column].astype(str).str.lower().str.contains(model_name, regex=False, na=False)]
                    if not matched.empty:
                        data = matched
                        break
        for arg_name, columns in {
            "top_k": ("top_k", "topk", "k"),
            "holding_period": ("holding_period", "hold_days", "holding_days"),
        }.items():
            raw = arguments.get(arg_name)
            if raw in (None, ""):
                continue
            for column in columns:
                if column in data.columns:
                    matched = data[pd.to_numeric(data[column], errors="coerce") == int(raw)]
                    if not matched.empty:
                        data = matched
                        break
        records = _jsonable(data.head(100))
        return {
            "success": True,
            "message": "Backtest summary queried.",
            "data": {"records": records, "summary": {"returned_count": len(records)}, "source_id": "backtest_master_table"},
            "warnings": [],
            "errors": [],
            "sources": [{"source_id": "backtest_master_table"}],
        }

    def get_strategy(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        value = _jsonable(web_read_service.selected_strategy())
        found = bool(value)
        return {
            "success": True,
            "message": "Selected strategy queried." if found else "No selected strategy is configured.",
            "data": {"found": found, "strategy": value or {}, "source_id": "selected_strategy"},
            "warnings": [], "errors": [], "sources": [{"source_id": "selected_strategy"}],
        }

    def get_portfolio(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        raw = invoke_worker_mcp(
            "data",
            "get_portfolio_state",
            {
                "user_id": str(
                    arguments.get("user_id")
                    or context.get("user_id")
                    or "default"
                )
            },
            context,
            caller_tool_id=INTERNAL_PORTFOLIO_GET_STATE,
        )
        if raw.get("success") is not True:
            return raw
        portfolio = dict(raw.get("data") or {})
        positions = [
            dict(item)
            for item in (portfolio.get("active_positions") or portfolio.get("positions") or [])
            if isinstance(item, dict)
        ]
        # Semantic state is deliberately separated from record-heavy position
        # collections. Large diagnostic snapshots/orders are not duplicated into
        # the Worker-to-Worker state Slot; dedicated Tools can expose them when a
        # future capability actually requires those facts.
        state = {
            key: value
            for key, value in portfolio.items()
            if key not in {
                "positions",
                "active_positions",
                "orders",
                "portfolio_snapshot",
                "calculation_trace",
            }
        }
        return {
            "success": True,
            "message": str(raw.get("message") or "Portfolio state queried."),
            "data": {
                "portfolio_state": _jsonable(state),
                "portfolio_positions": _jsonable(positions),
            },
            "warnings": list(raw.get("warnings") or []),
            "errors": list(raw.get("errors") or []),
            "sources": list(portfolio.get("sources") or []),
        }

    def get_account(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        raw = invoke_worker_mcp(
            "data",
            "get_portfolio_state",
            {
                "user_id": str(
                    arguments.get("user_id")
                    or context.get("user_id")
                    or "default"
                )
            },
            context,
            caller_tool_id=INTERNAL_ACCOUNT_GET_STATE,
        )
        value = dict(raw.get("data") or {})
        return {
            "success": bool(raw.get("success")),
            "message": str(raw.get("message") or "Account state queried."),
            "data": _jsonable(
                {
                    "account": value.get("account") or {},
                    "account_summary": value.get("account_summary") or {},
                    "as_of_date": value.get("as_of_date") or "",
                    "source_id": "database/portfolio",
                }
            ),
            "warnings": list(raw.get("warnings") or []),
            "errors": list(raw.get("errors") or []),
            "sources": list(value.get("sources") or []),
        }

    def get_profile(arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        raw = invoke_worker_mcp(
            "data",
            "get_user_profile",
            {
                "user_id": str(
                    arguments.get("user_id")
                    or context.get("user_id")
                    or "default"
                )
            },
            context,
            caller_tool_id=INTERNAL_USER_PROFILE_GET,
        )
        data = dict(raw.get("data") or {})
        constraints = data.pop("constraints", {})
        return {
            **raw,
            "data": {
                "profile_state": _jsonable(data),
                "constraints": _jsonable(constraints),
            },
        }

    specs = [
        {
            "name": INTERNAL_PREDICTION_GET_STOCK,
            "display": "Query Stock Prediction",
            "handler": get_prediction,
            "required": ["security_node_id"],
            "outputs": ["entity_model_signals"],
            "function": "Read model prediction and score facts for one already-resolved security. The returned record may contain that security's own rank field, but this Tool does not provide the stock-universe ranking list or TopK market ranking.",
            "applies": "W02 has an authoritative security_node_id and needs entity-specific prediction/score signals.",
            "not_for": "Stock-universe ranking or TopK ranking retrieval; use internal.ranking.get_latest for market ranking. Also not for discovering a security when no identity has been resolved yet.",
        },
        {
            "name": INTERNAL_RANKING_GET_LATEST,
            "display": "Query Latest Ranking",
            "handler": get_ranking,
            "required": [],
            "outputs": ["market_ranking_signals"],
            "function": "Read the latest model ranking across the configured stock universe.",
            "applies": "W02 needs market ranking facts or must discover a target security from ranking results.",
            "not_for": "Resolving a ranking row into GraphRef or reading a single security's detailed signal.",
        },
        {
            "name": INTERNAL_ENTITY_RESOLVE_RANKED_SECURITY,
            "display": "Resolve Ranked Security",
            "handler": resolve_ranked_security,
            "required": ["market_ranking_signals"],
            "outputs": ["selected_entity_ref", "security_node_id"],
            "function": "Resolve the leading security from structured ranking output into an authoritative GraphRef.",
            "applies": "A prior private Tool produced market_ranking_signals and downstream entity-specific Tools need identity.",
            "not_for": "Guessing securities from free text or bypassing Neo4j identity authority.",
        },
        {
            "name": INTERNAL_MODEL_GET_METRICS, "display": "Query Model Metrics", "handler": get_metrics,
            "required": [], "outputs": ["model_quality_metrics"],
            "function": "Read model quality metrics already stored by the application.",
            "applies": "W02 needs model-quality facts.", "not_for": "Ranking discovery or entity resolution.",
        },
        {
            "name": INTERNAL_BACKTEST_GET_SUMMARY, "display": "Query Backtest Summary", "handler": get_backtest,
            "required": [], "outputs": ["backtest_summary"],
            "function": "Read backtest summary records from the application's authoritative table.",
            "applies": "W02 needs backtest facts.", "not_for": "Live ranking or entity resolution.",
        },
        {
            "name": INTERNAL_STRATEGY_GET_SELECTED, "display": "Query Selected Strategy", "handler": get_strategy,
            "required": [], "outputs": ["selected_strategy_state"],
            "function": "Read the currently selected strategy state.",
            "applies": "W02 needs configured strategy facts.", "not_for": "Generating or changing a strategy.",
        },
        {
            "name": INTERNAL_PORTFOLIO_GET_STATE, "display": "Query Portfolio State", "handler": get_portfolio,
            "required": ["user_id"], "outputs": ["current_portfolio_state", "portfolio_positions"],
            "function": "Read the current paper portfolio and positions.",
            "applies": "W02 needs portfolio facts for the current user.", "not_for": "Portfolio writes or recommendations.",
        },
        {
            "name": INTERNAL_ACCOUNT_GET_STATE, "display": "Query Account State", "handler": get_account,
            "required": ["user_id"], "outputs": ["account_financial_state"],
            "function": "Read the current paper account financial state.",
            "applies": "W02 needs account facts for the current user.", "not_for": "Account mutation.",
        },
        {
            "name": INTERNAL_USER_PROFILE_GET, "display": "Query User Profile", "handler": get_profile,
            "required": ["user_id"], "outputs": ["user_profile_state", "user_constraints"],
            "function": "Read the saved user profile and constraints.",
            "applies": "W02 needs user-specific facts or constraints.", "not_for": "Changing user profile state.",
        },
    ]
    definitions: list[ToolDefinition] = []

    def io_contracts(tool_name: str) -> tuple[list[ToolInputContract], list[ToolOutputContract]]:
        if tool_name == INTERNAL_RANKING_GET_LATEST:
            return [], [
                ToolOutputContract(
                    slot_id="market_ranking_signals",
                    schema_id="RankingSignals.v1",
                    source_path="data",
                    description="Latest model ranking facts, including ranked records and snapshot metadata.",
                    contract="market.ranking",
                    version="1.0",
                )
            ]
        if tool_name == INTERNAL_ENTITY_RESOLVE_RANKED_SECURITY:
            return [
                ToolInputContract(
                    slot_id="market_ranking_signals",
                    schema_id="RankingSignals.v1",
                    required=True,
                    accepted_sources=("upstream_tool",),
                    description="Structured ranking facts from an upstream ranking Tool.",
                    contract="market.ranking",
                    version="1.0",
                )
            ], [
                ToolOutputContract(
                    slot_id="selected_entity_ref",
                    schema_id="GraphRef.v1",
                    source_path="data.selected_entity_ref",
                    description="Authoritative GraphRef for the selected ranked security.",
                    contract="graph.entity-ref",
                    version="1.0",
                ),
                ToolOutputContract(
                    slot_id="security_node_id",
                    schema_id="SecurityNodeId.v1",
                    source_path="data.security_node_id",
                    description="Authoritative graph node id consumed by entity-specific internal Tools.",
                    contract="graph.security-node-id",
                    version="1.0",
                ),
            ]
        if tool_name == INTERNAL_PREDICTION_GET_STOCK:
            return [
                ToolInputContract(
                    slot_id="security_node_id",
                    schema_id="SecurityNodeId.v1",
                    required=True,
                    accepted_sources=("context", "upstream_tool"),
                    description="Authoritative security node id from runtime context or entity resolution.",
                    contract="graph.security-node-id",
                    version="1.0",
                )
            ], [
                ToolOutputContract(
                    slot_id="entity_model_signals",
                    schema_id="EntityModelSignals.v1",
                    source_path="data",
                    description="Entity-specific model prediction/score facts for one authoritative security. This slot is not the stock-universe market ranking list.",
                    contract="model.stock-signals",
                    version="1.0",
                )
            ]
        if tool_name == INTERNAL_MODEL_GET_METRICS:
            return [], [
                ToolOutputContract(
                    slot_id="model_quality_metrics",
                    schema_id="ModelQualityMetrics.v1",
                    source_path="data",
                    description="Stored model quality and evaluation metrics.",
                )
            ]
        if tool_name == INTERNAL_BACKTEST_GET_SUMMARY:
            return [], [
                ToolOutputContract(
                    slot_id="backtest_summary",
                    source_path="data",
                    description="Backtest facts and summary records.",
                )
            ]
        if tool_name == INTERNAL_STRATEGY_GET_SELECTED:
            return [], [
                ToolOutputContract(
                    slot_id="selected_strategy_state",
                    source_path="data",
                    description="Current selected-strategy read state.",
                )
            ]
        if tool_name == INTERNAL_PORTFOLIO_GET_STATE:
            return [], [
                ToolOutputContract(
                    slot_id="current_portfolio_state",
                    schema_id="PortfolioState.v1",
                    source_path="data.portfolio_state",
                    description="Authoritative current portfolio snapshot.",
                    contract="portfolio.state",
                    version="1.0",
                ),
                ToolOutputContract(
                    slot_id="portfolio_positions",
                    schema_id="PortfolioPositions.v1",
                    source_path="data.portfolio_positions",
                    description="Current portfolio position records only.",
                    contract="portfolio.positions",
                    version="1.0",
                ),
            ]
        if tool_name == INTERNAL_ACCOUNT_GET_STATE:
            return [], [
                ToolOutputContract(
                    slot_id="account_financial_state",
                    schema_id="AccountFinancialState.v1",
                    source_path="data",
                    description="Current account financial summary.",
                    contract="portfolio.account",
                    version="1.0",
                )
            ]
        if tool_name == INTERNAL_USER_PROFILE_GET:
            return [], [
                ToolOutputContract(
                    slot_id="user_profile_state",
                    schema_id="UserProfileState.v1",
                    source_path="data.profile_state",
                    description="Current user profile, risk assessment and investment goal.",
                    contract="user.profile",
                    version="1.0",
                ),
                ToolOutputContract(
                    slot_id="user_constraints",
                    schema_id="UserConstraints.v1",
                    source_path="data.constraints",
                    description="Current explicit user constraints only.",
                    contract="user.constraints",
                    version="1.0",
                ),
            ]
        return [], []

    def mcp_runtime_policy(tool_name: str) -> dict[str, Any]:
        bindings = {
            INTERNAL_PREDICTION_GET_STOCK: ("model", "predict_stock_score"),
            INTERNAL_RANKING_GET_LATEST: ("model", "predict_rank"),
            INTERNAL_PORTFOLIO_GET_STATE: ("data", "get_portfolio_state"),
            INTERNAL_ACCOUNT_GET_STATE: ("data", "get_portfolio_state"),
            INTERNAL_USER_PROFILE_GET: ("data", "get_user_profile"),
        }
        binding = bindings.get(tool_name)
        if binding is None:
            return {}
        return {
            "provider_type": "mcp",
            "server_id": binding[0],
            "transport_tool_name": binding[1],
            "transport": "stdio",
            "projection_kind": "worker_adapter",
        }

    common_properties = {
        "security_node_id": {"type": "string"},
        "selected_entity_ref": {"type": "object"},
        "market_ranking_signals": {"type": "object"},
        "user_id": {"type": "string"},
        "top_k": {"type": "integer"},
        "model_name": {"type": "string"},
        "trade_date": {"type": "string"},
        "holding_period": {"type": "integer"},
        "as_of_time": {"type": "string"},
    }
    for spec in specs:
        required = list(spec["required"])
        outputs = list(spec["outputs"])
        input_contracts, output_contracts = io_contracts(str(spec["name"]))
        declared_input_ids = {item.slot_id for item in input_contracts}
        input_contracts = [
            *input_contracts,
            *[
                ToolInputContract(
                    slot_id=key,
                    required=key in required,
                    accepted_sources=("context", "upstream_tool"),
                )
                for key in common_properties
                if key not in declared_input_ids
            ],
        ]
        definitions.append(
            ToolDefinition(
                name=str(spec["name"]),
                display_name=str(spec["display"]),
                description=description(
                    str(spec["function"]),
                    str(spec["applies"]),
                    str(spec["not_for"]),
                    ", ".join(required) if required else "Worker runtime context and optional query parameters.",
                    ", ".join(outputs),
                ),
                input_schema=schema(common_properties, required=required),
                output_schema=result_schema(
                    ["security_node_id", "selected_entity_ref"]
                    if spec["name"] == INTERNAL_ENTITY_RESOLVE_RANKED_SECURITY
                    else []
                ),
                execution_handler=spec["handler"],
                supported_actions=[str(spec["name"]).rsplit(".", 1)[-1]],
                supported_objects=["internal_system_data"],
                produced_outputs=outputs,
                required_input_slots=required,
                optional_input_slots=[key for key in common_properties if key not in required],
                input_contracts=input_contracts,
                output_contracts=output_contracts,
                operation_type=OP_READ,
                allowed_agent_types=[PORTFOLIO_ANALYST],
                permission_scope=OP_READ,
                visibility=TOOL_VISIBILITY_WORKER_PRIVATE,
                side_effects=[],
                mutates_business_state=False,
                idempotency="read_only",
                audit_level="full",
                runtime_policy=mcp_runtime_policy(str(spec["name"])),
                tags=["worker_private", "internal_system", "read_only"],
            )
        )
    return definitions


__all__ = [
    "INTERNAL_PREDICTION_GET_STOCK",
    "INTERNAL_RANKING_GET_LATEST",
    "INTERNAL_ENTITY_RESOLVE_RANKED_SECURITY",
    "INTERNAL_MODEL_GET_METRICS",
    "INTERNAL_BACKTEST_GET_SUMMARY",
    "INTERNAL_STRATEGY_GET_SELECTED",
    "INTERNAL_PORTFOLIO_GET_STATE",
    "INTERNAL_ACCOUNT_GET_STATE",
    "INTERNAL_USER_PROFILE_GET",
    "build_internal_system_tool_definitions",
]
