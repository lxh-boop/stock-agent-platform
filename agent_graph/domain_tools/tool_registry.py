from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_graph.domain_tools.backfill_tool import execute_confirmed_backfill_plan, preview_backfill
from agent_graph.domain_tools.capital_management_tool import execute_confirmed_capital_plan, preview_capital_change
from agent_graph.domain_tools.manual_position_operation_tool import preview_manual_position_operation
from agent_graph.domain_tools.paper_trade_execute_tool import execute_confirmed_paper_trade_plan
from agent_graph.domain_tools.paper_trade_preview_tool import preview_paper_trade
from agent_graph.domain_tools.portfolio_risk_tool import query_portfolio_risk
from agent_graph.domain_tools.portfolio_state_tool import query_portfolio_state
from agent_graph.domain_tools.position_recommendation_tool import recommend_position_weight
from agent_graph.domain_tools.python_sandbox_tool import run_python_sandbox_analysis
from agent_graph.domain_tools.ranking_tool import query_ranking
from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper, preview_adjust_position_to_weight
from agent_graph.domain_tools.replacement_recommendation_tool import recommend_replacements
from agent_graph.domain_tools.report_tool import query_latest_reports
from agent_graph.domain_tools.scheduler_tool import query_scheduler_status
from agent_graph.domain_tools.strategy_builder_tool import prepare_strategy_change
from agent_graph.domain_tools.strategy_management_tool import execute_confirmed_strategy_plan, manage_strategy
from agent_graph.domain_tools.strategy_workflow_tools import (
    commit_strategy_apply_plan,
    commit_strategy_binding_plan,
    commit_current_strategy_position_change,
    create_strategy_activation_plan,
    create_strategy_binding_rollback_plan,
    create_strategy_apply_plan,
    preview_current_strategy_position_change,
    get_active_strategy_proposal,
    get_strategy_audit_trace,
    get_strategy_context,
    prepare_strategy_implementation,
    save_strategy_proposal_draft,
)
from agent_graph.domain_tools.stock_analysis_tool import analyze_stock
from agent_graph.domain_tools.stock_lookup_tool import lookup_stock
from agent_graph.domain_tools.stock_news_tool import query_stock_news
from agent_graph.domain_tools.stock_rag_tool import query_stock_rag
from agent_graph.domain_tools.tool_schemas import ToolPermission
from agent_graph.domain_tools.user_profile_tool import query_user_profile


class ToolCategory:
    READ_QUERY = "read_query"
    RAG_RETRIEVAL = "rag_retrieval"
    READ_ANALYSIS = "read_analysis"
    ACTION_PREVIEW = "action_preview"
    PROTECTED_EXECUTION = "protected_execution"


def _object_schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required or []),
        "additionalProperties": True,
    }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    permission: str
    description: str
    handler: Callable[..., Any]
    requires_confirmation: bool = False
    input_schema: dict[str, Any] = field(default_factory=_object_schema)
    output_schema: dict[str, Any] = field(default_factory=_object_schema)
    read_only: bool | None = None
    has_side_effect: bool | None = None
    concurrency_safe: bool | None = None
    idempotent: bool | None = None
    timeout_seconds: int = 9930
    retry_policy: dict[str, Any] = field(
        default_factory=lambda: {"max_attempts": 1, "backoff_seconds": 0.0}
    )
    result_retention: str = "summary"
    category: str = ToolCategory.READ_QUERY

    def __post_init__(self) -> None:
        read_only = self.permission == ToolPermission.READ if self.read_only is None else bool(self.read_only)
        has_side_effect = (
            self.permission != ToolPermission.READ or self.requires_confirmation
            if self.has_side_effect is None
            else bool(self.has_side_effect)
        )
        concurrency_safe = (
            read_only and not has_side_effect if self.concurrency_safe is None else bool(self.concurrency_safe)
        )
        idempotent = read_only and not has_side_effect if self.idempotent is None else bool(self.idempotent)
        object.__setattr__(self, "read_only", read_only)
        object.__setattr__(self, "has_side_effect", has_side_effect)
        object.__setattr__(self, "concurrency_safe", concurrency_safe)
        object.__setattr__(self, "idempotent", idempotent)

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission,
            "input_schema": deepcopy(self.input_schema),
            "output_schema": deepcopy(self.output_schema),
            "read_only": bool(self.read_only),
            "has_side_effect": bool(self.has_side_effect),
            "requires_confirmation": bool(self.requires_confirmation),
            "concurrency_safe": bool(self.concurrency_safe),
            "idempotent": bool(self.idempotent),
            "timeout_seconds": int(self.timeout_seconds),
            "retry_policy": deepcopy(self.retry_policy),
            "result_retention": self.result_retention,
            "category": self.category,
        }


_COMMON_INPUTS: dict[str, dict[str, Any]] = {
    "user_id": {"type": "string"},
    "stock_code": {"type": "string"},
    "top_k": {"type": "integer", "minimum": 1},
    "output_dir": {"type": "string"},
    "db_path": {"type": "string"},
    "session_id": {"type": "string"},
    "plan_id": {"type": "string"},
    "confirmation_token": {"type": "string"},
    "requested_weight": {"type": "number"},
    "position_adjustment_ratio": {"type": "number"},
    "requested_quantity": {"type": "number"},
    "query": {"type": "string"},
    "cash_weight": {"type": "number"},
    "target_position_count": {"type": "integer"},
    "flow_type": {"type": "string"},
    "amount": {"type": "number"},
    "effective_date": {"type": "string"},
    "start_date": {"type": "string"},
    "end_date": {"type": "string"},
    "code": {"type": "string"},
    "snapshot": {"type": "object"},
    "snapshot_id": {"type": "string"},
    "timeout_seconds": {"type": "number"},
    "max_output_chars": {"type": "integer"},
    "requirement": {"type": "string"},
    "parameters": {"type": "object"},
    "action": {"type": "string"},
    "strategy_id": {"type": "string"},
    "strategy_version": {"type": "string"},
    "account_id": {"type": "string"},
    "conversation_id": {"type": "string"},
    "conversation_action": {"type": "string"},
    "proposal_id": {"type": "string"},
    "commit_id": {"type": "string"},
    "binding_id": {"type": "string"},
    "proposal_json": {"type": "object"},
    "original_request": {"type": "string"},
    "user_feedback": {"type": "string"},
    "change_summary": {"type": "string"},
    "base_strategy_id": {"type": "string"},
    "base_strategy_version": {"type": "string"},
    "source_run_id": {"type": "string"},
    "proposal_version": {"type": "integer"},
    "run_id": {"type": "string"},
    "implementation_id": {"type": "string"},
    "effective_from": {"type": "string"},
    "recommendations": {
        "type": "array",
        "items": {"type": "object"},
    },
    "trade_date": {"type": "string"},
}


def _schema_for(*names: str, required: list[str] | None = None) -> dict[str, Any]:
    return _object_schema({name: _COMMON_INPUTS[name] for name in names}, required=required)


def _result_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "success": {"type": "boolean"},
            "message": {"type": "string"},
            "data": {"type": "object"},
            "warnings": {"type": "array"},
            "errors": {"type": "array"},
            "requires_confirmation": {"type": "boolean"},
            "confirmation_token": {"type": "string"},
        }
    )


_SIMPLE_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


def validate_tool_args(tool_name: str, args: dict[str, Any] | None) -> tuple[bool, list[str]]:
    spec = get_tool_registry().get(tool_name)
    if spec is None:
        return False, ["unregistered_tool"]
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return False, ["tool_args_must_be_object"]

    errors: list[str] = []
    schema = spec.input_schema or {}
    properties = schema.get("properties") or {}
    for required_name in schema.get("required") or []:
        if required_name not in args or args.get(required_name) in (None, ""):
            errors.append(f"missing_required:{required_name}")

    for name, value in args.items():
        if value is None or name not in properties:
            continue
        wanted = properties.get(name, {}).get("type")
        allowed = _SIMPLE_TYPE_MAP.get(str(wanted or ""))
        if allowed and not isinstance(value, allowed):
            errors.append(f"invalid_type:{name}:{wanted}")
    return not errors, errors


def _spec(
    name: str,
    permission: str,
    description: str,
    handler: Callable[..., Any],
    requires_confirmation: bool = False,
    *,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    category: str | None = None,
    timeout_seconds: int = 9930,
    retry_policy: dict[str, Any] | None = None,
    result_retention: str = "summary",
) -> ToolSpec:
    if category is None:
        category = (
            ToolCategory.PROTECTED_EXECUTION
            if permission == ToolPermission.WRITE
            else ToolCategory.ACTION_PREVIEW
            if permission == ToolPermission.PREVIEW
            else ToolCategory.READ_QUERY
        )
    return ToolSpec(
        name,
        permission,
        description,
        handler,
        requires_confirmation,
        input_schema=input_schema or _object_schema(),
        output_schema=output_schema or _result_schema(),
        category=category,
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy or {"max_attempts": 1, "backoff_seconds": 0.0},
        result_retention=result_retention,
    )


def get_tool_registry(
    *,
    include_mcp: bool = False,
    mcp_context: dict[str, Any] | None = None,
) -> dict[str, ToolSpec]:
    # Raw MCP capabilities are system-private. The existing signature is kept
    # for callers, but Runtime projects only registered Worker Tool IDs.
    del include_mcp, mcp_context
    specs = [
        _spec("stock_lookup", ToolPermission.READ, "Lookup a stock in latest ranking/recommendations.", lookup_stock, input_schema=_schema_for("stock_code", "top_k", "output_dir")),
        _spec("stock_analysis", ToolPermission.READ, "Analyze ranking, AI adjustment, news, RAG, user suitability.", analyze_stock, input_schema=_schema_for("user_id", "stock_code", "top_k", "output_dir", "db_path"), category=ToolCategory.READ_ANALYSIS),
        _spec("stock_news", ToolPermission.READ, "Query mapped news events for a stock.", query_stock_news, input_schema=_schema_for("stock_code", "db_path")),
        _spec("stock_rag", ToolPermission.READ, "Query RAG chunks for a stock.", query_stock_rag, input_schema=_schema_for("stock_code", "output_dir"), category=ToolCategory.RAG_RETRIEVAL),
        _spec("ranking", ToolPermission.READ, "Read latest ranking or a stock row.", query_ranking, input_schema=_schema_for("stock_code", "top_k", "output_dir")),
        _spec("user_profile", ToolPermission.READ, "Read user risk profile and constraints.", query_user_profile, input_schema=_schema_for("user_id", "output_dir", "db_path")),
        _spec("portfolio_state", ToolPermission.READ, "Read paper account, positions, and orders.", query_portfolio_state, input_schema=_schema_for("user_id", "output_dir", "db_path")),
        _spec("portfolio_risk", ToolPermission.READ, "Read or compute portfolio risk.", query_portfolio_risk, input_schema=_schema_for("user_id", "output_dir", "db_path"), category=ToolCategory.READ_ANALYSIS),
        _spec("position_recommendation", ToolPermission.READ, "Recommend a paper target weight.", recommend_position_weight, input_schema=_schema_for("user_id", "stock_code", "requested_weight", "top_k", "output_dir", "db_path"), category=ToolCategory.READ_ANALYSIS),
        _spec("replacement_recommendation", ToolPermission.READ, "Rank existing positions for possible replacement.", recommend_replacements, input_schema=_schema_for("user_id", "stock_code", "requested_weight", "output_dir", "db_path"), category=ToolCategory.READ_ANALYSIS),
        _spec("manual_position_operation_tool", ToolPermission.PREVIEW, "Preview a one-time paper-position operation without changing long-term strategy.", preview_manual_position_operation, True, input_schema=_schema_for("user_id", "stock_code", "requested_weight", "position_adjustment_ratio", "requested_quantity", "cash_weight", "target_position_count", "query", "top_k", "output_dir", "db_path", "session_id", required=["user_id"]), result_retention="full"),
        _spec("strategy.get_context", ToolPermission.READ, "Read the exact user/account/conversation strategy discussion context.", get_strategy_context, input_schema=_schema_for("user_id", "account_id", "conversation_id", "output_dir", "db_path", required=["user_id"]), result_retention="full"),
        _spec("strategy.get_active_proposal", ToolPermission.READ, "Read the active versioned strategy proposal in the exact user/account/conversation scope.", get_active_strategy_proposal, input_schema=_schema_for("user_id", "account_id", "conversation_id", "db_path", required=["user_id"]), result_retention="full"),
        _spec("strategy.get_audit_trace", ToolPermission.READ, "Reconstruct a redacted strategy lifecycle audit trace from stable identifiers.", get_strategy_audit_trace, input_schema=_schema_for("user_id", "proposal_id", "implementation_id", "plan_id", "commit_id", "binding_id", "run_id", "conversation_id", "output_dir", "db_path", required=["user_id"]), result_retention="full"),
        _spec("strategy.save_proposal_draft", ToolPermission.PREVIEW, "Save the exact LLM-authored strategy draft without creating a formal confirmation plan.", save_strategy_proposal_draft, False, input_schema=_schema_for("user_id", "account_id", "conversation_id", "conversation_action", "proposal_id", "proposal_json", "original_request", "user_feedback", "change_summary", "base_strategy_id", "base_strategy_version", "source_run_id", "db_path", required=["user_id", "conversation_action"]), result_retention="full"),
        _spec("strategy.prepare_implementation", ToolPermission.PREVIEW, "Lock an exact Proposal version and generate isolated implementation artifacts only.", prepare_strategy_implementation, False, input_schema=_schema_for("proposal_id", "proposal_version", "user_id", "account_id", "conversation_id", "run_id", "db_path", required=["proposal_id", "proposal_version", "user_id", "account_id", "conversation_id", "run_id"]), result_retention="full"),
        _spec("strategy.create_apply_plan", ToolPermission.PREVIEW, "Create a confirmation-required hash-bound plan for applying a validated implementation.", create_strategy_apply_plan, True, input_schema=_schema_for("implementation_id", "user_id", "account_id", "conversation_id", "run_id", "output_dir", "db_path", required=["implementation_id", "user_id", "account_id", "conversation_id", "run_id"]), result_retention="full"),
        _spec("strategy.apply.commit", ToolPermission.WRITE, "Commit a confirmed strategy implementation after full hash revalidation.", commit_strategy_apply_plan, True, input_schema=_schema_for("user_id", "plan_id", "confirmation_token", "conversation_id", "output_dir", "db_path", required=["user_id", "plan_id", "confirmation_token"]), result_retention="full"),
        _spec("strategy.create_activation_plan", ToolPermission.PREVIEW, "Create an account-scoped future strategy activation confirmation plan.", create_strategy_activation_plan, True, input_schema=_schema_for("user_id", "account_id", "strategy_id", "strategy_version", "effective_from", "conversation_id", "run_id", "output_dir", "db_path", required=["user_id", "account_id", "strategy_id", "strategy_version"]), result_retention="full"),
        _spec("strategy.create_binding_rollback_plan", ToolPermission.PREVIEW, "Create a confirmation plan to restore the previous account strategy binding.", create_strategy_binding_rollback_plan, True, input_schema=_schema_for("user_id", "account_id", "conversation_id", "run_id", "output_dir", "db_path", required=["user_id", "account_id"]), result_retention="full"),
        _spec("strategy.binding.commit", ToolPermission.WRITE, "Commit a confirmed account-scoped strategy activation or rollback.", commit_strategy_binding_plan, True, input_schema=_schema_for("user_id", "plan_id", "confirmation_token", "conversation_id", "output_dir", "db_path", required=["user_id", "plan_id", "confirmation_token"]), result_retention="full"),
        _spec("strategy.preview_current_position_change", ToolPermission.PREVIEW, "Preview applying the effective strategy Binding to current paper positions.", preview_current_strategy_position_change, True, input_schema=_schema_for("user_id", "account_id", "recommendations", "trade_date", "conversation_id", "run_id", "output_dir", "db_path", required=["user_id", "account_id"]), result_retention="full"),
        _spec("strategy.position.commit", ToolPermission.WRITE, "Commit a confirmed current-position strategy change after account and Binding revalidation.", commit_current_strategy_position_change, True, input_schema=_schema_for("user_id", "plan_id", "confirmation_token", "conversation_id", "output_dir", "db_path", required=["user_id", "plan_id", "confirmation_token"]), result_retention="full"),
        _spec("strategy_builder_tool", ToolPermission.PREVIEW, "Preview a long-term strategy change and prepare registration confirmation.", prepare_strategy_change, True, input_schema=_schema_for("user_id", "requirement", "parameters", "output_dir", "db_path", "session_id", required=["user_id", "requirement"]), result_retention="full"),
        _spec("strategy_management_tool", ToolPermission.PREVIEW, "List, enable, disable, switch, or prepare confirmation for paper strategies.", manage_strategy, True, input_schema=_schema_for("user_id", "action", "strategy_id", "strategy_version", "output_dir", "db_path", "session_id", required=["user_id", "action"]), result_retention="full"),
        _spec("rebalance_plan", ToolPermission.PREVIEW, "Create a confirmation-required rebalance preview.", preview_add_stock_to_paper, True, input_schema=_schema_for("user_id", "stock_code", "requested_weight", "top_k", "output_dir", "db_path", "session_id", required=["user_id", "stock_code"]), result_retention="full"),
        _spec("adjust_position", ToolPermission.PREVIEW, "Create a confirmation-required preview to adjust an existing paper position.", preview_adjust_position_to_weight, True, input_schema=_schema_for("user_id", "stock_code", "requested_weight", "position_adjustment_ratio", "requested_quantity", "top_k", "output_dir", "db_path", "session_id", required=["user_id", "stock_code"]), result_retention="full"),
        _spec("paper_trade_preview", ToolPermission.PREVIEW, "Preview a paper trade plan.", preview_paper_trade, True, input_schema=_schema_for("user_id", "stock_code", "requested_weight", "top_k", "output_dir", "db_path", "session_id", required=["user_id", "stock_code"]), result_retention="full"),
        _spec("paper_trade_execute", ToolPermission.WRITE, "Execute a confirmed paper-trading plan.", execute_confirmed_paper_trade_plan, True, input_schema=_schema_for("user_id", "plan_id", "confirmation_token", "output_dir", "db_path", "session_id", required=["user_id", "plan_id", "confirmation_token"]), result_retention="full"),
        _spec("paper_trading_execution_tool", ToolPermission.WRITE, "Execute a confirmed paper-trading plan through the protected gateway.", execute_confirmed_paper_trade_plan, True, input_schema=_schema_for("user_id", "plan_id", "confirmation_token", "output_dir", "db_path", "session_id", required=["user_id", "plan_id", "confirmation_token"]), result_retention="full"),
        _spec("strategy_confirmation_execute", ToolPermission.WRITE, "Execute a confirmed strategy registration or activation plan.", execute_confirmed_strategy_plan, True, input_schema=_schema_for("user_id", "plan_id", "confirmation_token", "output_dir", "db_path", "session_id", required=["user_id", "plan_id", "confirmation_token"]), result_retention="full"),
        _spec("capital_management_preview", ToolPermission.PREVIEW, "Preview a paper capital flow.", preview_capital_change, True, input_schema=_schema_for("user_id", "flow_type", "amount", "effective_date", "output_dir", "db_path", "session_id", required=["user_id", "flow_type", "amount"]), result_retention="full"),
        _spec("capital_management_execute", ToolPermission.WRITE, "Execute a confirmed paper capital flow.", execute_confirmed_capital_plan, True, input_schema=_schema_for("user_id", "plan_id", "confirmation_token", "output_dir", "db_path", "session_id", required=["user_id", "plan_id", "confirmation_token"]), result_retention="full"),
        _spec("scheduler_status", ToolPermission.READ, "Read scheduler status and latest log tail.", query_scheduler_status, input_schema=_schema_for("output_dir")),
        _spec("python_sandbox_analysis", ToolPermission.READ, "Run limited read-only Python analysis over an explicit task snapshot.", run_python_sandbox_analysis, input_schema=_schema_for("code", "snapshot", "snapshot_id", "timeout_seconds", "max_output_chars", required=["code"]), category=ToolCategory.READ_ANALYSIS, timeout_seconds=9910, retry_policy={"max_attempts": 1, "backoff_seconds": 0.0}, result_retention="summary"),
        _spec("backfill_preview", ToolPermission.PREVIEW, "Preview paper-trading backfill.", preview_backfill, True, input_schema=_schema_for("user_id", "start_date", "end_date", "output_dir", "db_path", "session_id", required=["user_id", "start_date"]), result_retention="full"),
        _spec("backfill_execute", ToolPermission.WRITE, "Execute confirmed paper-trading backfill.", execute_confirmed_backfill_plan, True, input_schema=_schema_for("user_id", "plan_id", "confirmation_token", "output_dir", "db_path", "session_id", required=["user_id", "plan_id", "confirmation_token"]), result_retention="full"),
        _spec("report", ToolPermission.READ, "List latest generated reports.", query_latest_reports, input_schema=_schema_for("output_dir")),
    ]
    registry = {spec.name: spec for spec in specs}
    return registry


def list_tools(
    *,
    include_mcp: bool = False,
    mcp_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        spec.metadata()
        for spec in get_tool_registry(include_mcp=include_mcp, mcp_context=mcp_context).values()
    ]
