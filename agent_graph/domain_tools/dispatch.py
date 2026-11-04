"""Deterministic tool dispatch for the paper-trading commit surface.

Ten live tools only: the paper-trading approval-commit gateway
(``writes/gateway.py``) and the two REST preview endpoints. The former 51-tool
registry carried 41 definitions with no caller; those died with the legacy
agent runtime. The main agent's read tools do NOT pass through here — they
come from ``toolbox/worker_tools`` via ``tools/main_tools.build_read_tools``.
"""

from __future__ import annotations

from typing import Any

from agent_graph.domain_tools.portfolio_proposal_adapters import portfolio_commit_paper_trade_adapter
from agent_graph.domain_tools.write_operation_adapters import (
    approval_confirm_plan_adapter,
    backfill_commit_adapter,
    backfill_preview_adapter,
    capital_change_commit_adapter,
    capital_change_preview_adapter,
    strategy_apply_commit_adapter,
    strategy_binding_commit_adapter,
    strategy_disable_commit_adapter,
    strategy_position_commit_adapter,
)
from agent_graph.toolbox.contracts import (
    AGENT_MAIN,
    AGENT_READ,
    AGENT_WRITE,
    OP_PROPOSAL,
    OP_READ,
    OP_WRITE,
    ToolDefinition,
    UnifiedToolResult,
)
from agent_graph.toolbox.registry import ToolRegistry
from agent_graph.toolbox.validation import (
    description as _description,
    normalise_raw_result as _normalise_raw_result,
    result_schema as _result_schema,
    safe_argument_keys as _safe_argument_keys,
    schema as _schema,
    validate_input as _validate_input,
)


def build_core_tool_definitions() -> list[ToolDefinition]:
    """The live commit/preview surface. Order is irrelevant; the registry indexes by name."""
    return [
ToolDefinition(
                name="portfolio.commit_paper_trade",
                display_name="Portfolio Paper Trade Commit",
                description=_description(
                    "Commit a confirmed paper-trading plan after token, plan hash, state and trading-day revalidation.",
                    "A pending execute_add_stock or execute_adjust_position plan has a valid user confirmation token.",
                    "Previewing paper trades, creating new proposals, or any unapproved write.",
                    "user_id, plan_id, confirmation_token, output_dir, db_path.",
                    "revalidation_result, commit_result, audit_record.",
                    "Writes paper account state only after approval, revalidation and idempotency checks.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "confirmation_token": {"type": "string"},
                    },
                    required=["plan_id", "confirmation_token"],
                ),
                output_schema=_result_schema(),
                execution_handler=portfolio_commit_paper_trade_adapter,
                supported_actions=["execute_confirmed_plan", "commit_write_operation"],
                supported_objects=["current_portfolio", "paper_account"],
                produced_outputs=["revalidation_result", "commit_result", "audit_record"],
                operation_type=OP_WRITE,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_WRITE,
                requires_approval=True,
                aliases=["paper_trade_execute", "paper_trading_execution_tool"],
            ),

ToolDefinition(
                name="strategy.apply.commit",
                display_name="Commit Strategy Implementation",
                description=_description(
                    "Revalidate every Proposal, artifact, report and formal-baseline hash, then transactionally add a new strategy version and register it disabled.",
                    "WriteGateway dispatches a confirmed apply_strategy_implementation plan.",
                    "Unconfirmed calls, activation, position changes, or overwriting an existing strategy file.",
                    "user_id, plan_id and confirmation_token.",
                    "commit, registered-disabled manifest and audit identifiers.",
                    "Adds only a new formal strategy version after approval; rolls back file and Registry on failure.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "confirmation_token": {"type": "string"},
                    },
                    required=["plan_id", "confirmation_token"],
                ),
                output_schema=_result_schema(["commit_id", "strategy_manifest"]),
                execution_handler=strategy_apply_commit_adapter,
                supported_actions=["commit_write_operation"],
                supported_objects=["strategy_implementation", "strategy_registry"],
                produced_outputs=[
                    "revalidation_result",
                    "commit_result",
                    "audit_record",
                ],
                operation_type=OP_WRITE,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_WRITE,
                requires_approval=True,
            ),

ToolDefinition(
                name="strategy.binding.commit",
                display_name="Commit Account Strategy Binding",
                description=_description(
                    "Commit a confirmed account-scoped strategy activation or rollback after Binding and config-hash revalidation.",
                    "WriteGateway dispatches an activation/rollback plan with a valid confirmation.",
                    "Global strategy enablement or current position changes.",
                    "user_id, plan_id and confirmation_token.",
                    "binding record, commit and audit identifiers.",
                    "Writes one account Binding history event; creates no orders and changes no positions.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "confirmation_token": {"type": "string"},
                    },
                    required=["plan_id", "confirmation_token"],
                ),
                output_schema=_result_schema(["binding", "commit_id"]),
                execution_handler=strategy_binding_commit_adapter,
                supported_actions=["commit_write_operation"],
                supported_objects=["strategy_binding", "paper_account"],
                produced_outputs=[
                    "revalidation_result",
                    "commit_result",
                    "audit_record",
                ],
                operation_type=OP_WRITE,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_WRITE,
                requires_approval=True,
            ),

ToolDefinition(
                name="strategy.position.commit",
                display_name="Commit Current Strategy Position Change",
                description=_description(
                    "Revalidate account state and Binding config hash, then run the original paper pipeline for a confirmed current-position strategy change.",
                    "WriteGateway dispatches a valid execute_strategy_position_change plan.",
                    "Unconfirmed execution, strategy registration or Binding mutation.",
                    "user_id, plan_id and confirmation_token.",
                    "paper orders, strategy metadata, snapshots and commit identifiers.",
                    "Writes only after portfolio-specific confirmation and preserves before/after history.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "confirmation_token": {"type": "string"},
                    },
                    required=["plan_id", "confirmation_token"],
                ),
                output_schema=_result_schema(["commit_id", "order_ids"]),
                execution_handler=strategy_position_commit_adapter,
                supported_actions=["commit_write_operation"],
                supported_objects=[
                    "paper_account",
                    "paper_position",
                    "paper_order",
                ],
                produced_outputs=[
                    "revalidation_result",
                    "commit_result",
                    "audit_record",
                ],
                operation_type=OP_WRITE,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_WRITE,
                requires_approval=True,
            ),

ToolDefinition(
                name="strategy.disable.commit",
                display_name="Strategy Disable Commit",
                description=_description(
                    "Commit a confirmed strategy disable plan after revalidation.",
                    "A pending disable_strategy plan has a valid confirmation token.",
                    "Previewing changes, registering strategies, or modifying paper orders.",
                    "user_id, plan_id, confirmation_token, output_dir, db_path.",
                    "commit_result, revalidation_result, audit_record.",
                    "Writes strategy registry state only after confirmation and revalidation.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "confirmation_token": {"type": "string"},
                    },
                    required=["plan_id", "confirmation_token"],
                ),
                output_schema=_result_schema(),
                execution_handler=strategy_disable_commit_adapter,
                supported_actions=["commit_write_operation"],
                supported_objects=["strategy"],
                produced_outputs=["commit_result", "revalidation_result", "audit_record"],
                operation_type=OP_WRITE,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_WRITE,
                requires_approval=True,
                aliases=["strategy_disable_commit"],
            ),

ToolDefinition(
                name="capital.change.preview",
                display_name="Capital Change Preview",
                description=_description(
                    "Create a confirmation proposal for a paper cash-flow change.",
                    "A user submits deposit or withdrawal in the paper-trading account.",
                    "Executing the cash-flow write, paper order execution, or historical replay.",
                    "user_id, flow_type, amount, effective_date, reason, output_dir, db_path.",
                    "operation_preview, confirmation_request, risk_impact.",
                    "Creates an approval proposal only; no cash flow is saved.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "flow_type": {"type": "string"},
                        "amount": {"type": "number"},
                        "effective_date": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    required=["flow_type", "amount"],
                ),
                output_schema=_result_schema(),
                execution_handler=capital_change_preview_adapter,
                supported_actions=["preview_write_operation"],
                supported_objects=["paper_account"],
                produced_outputs=["operation_preview", "confirmation_request", "risk_impact"],
                operation_type=OP_PROPOSAL,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_PROPOSAL,
                aliases=["capital_management_preview"],
            ),

ToolDefinition(
                name="capital.change.commit",
                display_name="Capital Change Commit",
                description=_description(
                    "Commit a confirmed paper cash-flow change after account-state revalidation.",
                    "A pending capital_change plan has a valid confirmation token.",
                    "Previewing cash flow, paper order execution, or historical replay.",
                    "user_id, plan_id, confirmation_token, output_dir, db_path.",
                    "commit_result, revalidation_result, audit_record.",
                    "Writes one paper cash flow only after confirmation and revalidation.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "confirmation_token": {"type": "string"},
                    },
                    required=["plan_id", "confirmation_token"],
                ),
                output_schema=_result_schema(),
                execution_handler=capital_change_commit_adapter,
                supported_actions=["commit_write_operation"],
                supported_objects=["paper_account"],
                produced_outputs=["commit_result", "revalidation_result", "audit_record"],
                operation_type=OP_WRITE,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_WRITE,
                requires_approval=True,
                aliases=["capital_management_execute"],
            ),

ToolDefinition(
                name="backfill.preview",
                display_name="Backfill Preview",
                description=_description(
                    "Create a confirmation proposal for paper-trading historical backfill.",
                    "A user requests rebuilding or resuming paper-trading historical replay.",
                    "Executing replay immediately, changing cash flow, or modifying strategy registry.",
                    "user_id, start_date, end_date, initial_cash, force, resume, strategy options.",
                    "operation_preview, confirmation_request, risk_impact.",
                    "Creates an approval proposal only; no backfill is executed.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "start_date": {"type": "string"},
                        "end_date": {"type": "string"},
                        "initial_cash": {"type": "number"},
                        "force": {"type": "boolean"},
                        "resume": {"type": "boolean"},
                    },
                    required=["start_date"],
                ),
                output_schema=_result_schema(),
                execution_handler=backfill_preview_adapter,
                supported_actions=["preview_write_operation"],
                supported_objects=["paper_account"],
                produced_outputs=["operation_preview", "confirmation_request", "risk_impact"],
                operation_type=OP_PROPOSAL,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_PROPOSAL,
                aliases=["backfill_preview"],
            ),

ToolDefinition(
                name="backfill.commit",
                display_name="Backfill Commit",
                description=_description(
                    "Commit a confirmed paper-trading backfill plan after state revalidation.",
                    "A pending paper_backfill plan has a valid confirmation token.",
                    "Previewing replay, changing cash flow, or modifying strategy registry.",
                    "user_id, plan_id, confirmation_token, output_dir, db_path.",
                    "commit_result, revalidation_result, audit_record.",
                    "Runs paper-trading backfill only after confirmation and revalidation.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "confirmation_token": {"type": "string"},
                    },
                    required=["plan_id", "confirmation_token"],
                ),
                output_schema=_result_schema(),
                execution_handler=backfill_commit_adapter,
                supported_actions=["commit_write_operation"],
                supported_objects=["paper_account"],
                produced_outputs=["commit_result", "revalidation_result", "audit_record"],
                operation_type=OP_WRITE,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_WRITE,
                requires_approval=True,
                aliases=["backfill_execute"],
            ),

ToolDefinition(
                name="approval.confirm_plan",
                display_name="Approval Confirm Plan",
                description=_description(
                    "Confirm an existing protected action plan through the unified write gateway.",
                    "A user submits plan_id and confirmation_token for a supported pending plan.",
                    "Creating previews, unsupported plan types, or direct unapproved writes.",
                    "user_id, plan_id, confirmation_token, output_dir, db_path.",
                    "commit_result, revalidation_result, audit_record.",
                    "Dispatches to the correct protected writer only after approval is granted.",
                ),
                input_schema=_schema(
                    {
                        "user_id": {"type": "string"},
                        "plan_id": {"type": "string"},
                        "confirmation_token": {"type": "string"},
                    },
                    required=["plan_id", "confirmation_token"],
                ),
                output_schema=_result_schema(),
                execution_handler=approval_confirm_plan_adapter,
                supported_actions=["commit_write_operation"],
                supported_objects=["approval"],
                produced_outputs=["commit_result", "revalidation_result", "audit_record"],
                operation_type=OP_WRITE,
                allowed_agent_types=[AGENT_MAIN, AGENT_WRITE],
                permission_scope=OP_WRITE,
                requires_approval=True,
                aliases=["strategy_confirmation_execute"],
            ),
    ]


_GLOBAL_TOOL_REGISTRY_V2: ToolRegistry | None = None


def get_tool_registry_v2() -> ToolRegistry:
    global _GLOBAL_TOOL_REGISTRY_V2
    if _GLOBAL_TOOL_REGISTRY_V2 is None:
        _GLOBAL_TOOL_REGISTRY_V2 = ToolRegistry(build_core_tool_definitions())
    return _GLOBAL_TOOL_REGISTRY_V2


def _failure_result(
    tool_name: str,
    error_type: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> UnifiedToolResult:
    from datetime import datetime

    started = datetime.now().isoformat(timespec="seconds")
    return UnifiedToolResult(
        success=False,
        tool_name=tool_name,
        message=message,
        data=dict(data or {}),
        errors=[error_type],
        error_type=error_type,
        error_message=message,
        started_at=started,
        finished_at=started,
        duration_ms=0.0,
    )


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    context: dict[str, Any] | None = None,
    agent_type: str = AGENT_READ,
    approval_granted: bool = False,
) -> UnifiedToolResult:
    """Dispatch one registered business tool deterministically (no LLM, no orchestration)."""

    definition = get_tool_registry_v2().get(tool_name)
    if definition is None:
        return _failure_result(str(tool_name), "unknown_tool", f"Unknown tool: {tool_name}")
    allowed = definition.allowed_agent_types or [AGENT_READ]
    if agent_type not in allowed:
        return _failure_result(
            str(tool_name),
            "agent_not_allowed",
            f"Tool {tool_name} does not allow agent_type {agent_type}",
        )
    if bool(definition.requires_approval) and not approval_granted:
        return _failure_result(str(tool_name), "approval_required", f"Tool {tool_name} requires prior approval")
    args = dict(arguments or {})
    input_errors = _validate_input(definition, args)
    if input_errors:
        return _failure_result(
            str(tool_name),
            "invalid_arguments",
            "Invalid arguments: " + "; ".join(input_errors[:6]),
        )
    try:
        raw = definition.execution_handler(args, dict(context or {}))
    except Exception as exc:  # noqa: BLE001 - deterministic failure contract
        return _failure_result(str(tool_name), "tool_execution_failed", str(exc)[:400])
    normalised = _normalise_raw_result(raw, requested_name=str(tool_name), canonical_name=str(tool_name))
    return UnifiedToolResult(
        success=bool(normalised.get("success")),
        tool_name=str(normalised.get("tool_name") or tool_name),
        data=dict(normalised.get("data") or {}),
        message=str(normalised.get("message") or ""),
        warnings=[str(item) for item in normalised.get("warnings") or []],
        errors=[str(item) for item in normalised.get("errors") or []],
        error_type=str(normalised.get("error_type") or ""),
        error_message=str(normalised.get("error_message") or ""),
        metadata=dict(normalised.get("metadata") or {}),
    )
