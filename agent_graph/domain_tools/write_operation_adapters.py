from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.write_operation_service import write_operation_service
from agent_graph.domain_tools.strategy_builder_tool import prepare_strategy_change
from agent_graph.domain_tools.strategy_management_tool import manage_strategy
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


def _context_value(args: dict[str, Any], context: dict[str, Any], key: str, default: Any = None) -> Any:
    value = args.get(key)
    if value not in (None, ""):
        return value
    value = context.get(key)
    return default if value in (None, "") else value


def _output_dir(context: dict[str, Any]) -> str | Path:
    return context.get("output_dir") or "outputs"


def _db_path(context: dict[str, Any]) -> str | Path | None:
    return context.get("db_path")


def _session_id(context: dict[str, Any]) -> str:
    return str(context.get("session_id") or context.get("conversation_id") or "")


def strategy_get_context_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    user_id = str(_context_value(args, context, "user_id", "default"))
    return get_strategy_context(
        user_id=user_id,
        account_id=str(
            _context_value(args, context, "account_id", f"paper_{user_id}")
        ),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )


def strategy_get_active_proposal_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    user_id = str(_context_value(args, context, "user_id", "default"))
    return get_active_strategy_proposal(
        user_id=user_id,
        account_id=str(
            _context_value(args, context, "account_id", f"paper_{user_id}")
        ),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        db_path=_db_path(context),
    )


def strategy_get_audit_trace_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    return get_strategy_audit_trace(
        user_id=str(_context_value(args, context, "user_id", "default")),
        proposal_id=str(args.get("proposal_id") or ""),
        implementation_id=str(args.get("implementation_id") or ""),
        plan_id=str(args.get("plan_id") or ""),
        commit_id=str(args.get("commit_id") or ""),
        binding_id=str(args.get("binding_id") or ""),
        run_id=str(_context_value(args, context, "run_id", "")),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )


def strategy_save_proposal_draft_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    user_id = str(_context_value(args, context, "user_id", "default"))
    proposal_json = args.get("proposal_json")
    return save_strategy_proposal_draft(
        user_id=user_id,
        account_id=str(
            _context_value(args, context, "account_id", f"paper_{user_id}")
        ),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        original_request=str(
            args.get("original_request")
            or args.get("requirement")
            or context.get("raw_query")
            or context.get("query")
            or ""
        ),
        proposal_json=proposal_json if isinstance(proposal_json, dict) else {},
        user_feedback=str(args.get("user_feedback") or ""),
        change_summary=str(args.get("change_summary") or ""),
        conversation_action=str(
            args.get("conversation_action") or "save_proposal"
        ),
        proposal_id=str(args.get("proposal_id") or ""),
        base_strategy_id=str(
            args.get("base_strategy_id") or "hierarchical_top10"
        ),
        base_strategy_version=str(
            args.get("base_strategy_version") or "1.0.0"
        ),
        source_run_id=str(
            args.get("source_run_id")
            or args.get("run_id")
            or context.get("run_id")
            or ""
        ),
        db_path=_db_path(context),
    )


def strategy_prepare_implementation_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    return prepare_strategy_implementation(
        proposal_id=str(args.get("proposal_id") or ""),
        proposal_version=int(args.get("proposal_version") or 0),
        user_id=str(_context_value(args, context, "user_id", "default")),
        account_id=str(_context_value(args, context, "account_id", "")),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        run_id=str(_context_value(args, context, "run_id", "")),
        db_path=_db_path(context),
        runtime_dir=context.get("runtime_dir") or "runtime",
        project_root=context.get("root") or ".",
    )


def strategy_create_apply_plan_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    return create_strategy_apply_plan(
        implementation_id=str(args.get("implementation_id") or ""),
        user_id=str(_context_value(args, context, "user_id", "default")),
        account_id=str(_context_value(args, context, "account_id", "")),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        run_id=str(_context_value(args, context, "run_id", "")),
        db_path=_db_path(context),
        output_dir=_output_dir(context),
        runtime_dir=context.get("runtime_dir") or "runtime",
        project_root=context.get("root") or ".",
    )


def strategy_apply_commit_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    return commit_strategy_apply_plan(
        user_id=str(_context_value(args, context, "user_id", "default")),
        plan_id=str(args.get("plan_id") or ""),
        confirmation_token=str(args.get("confirmation_token") or ""),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        db_path=_db_path(context),
        output_dir=_output_dir(context),
        runtime_dir=context.get("runtime_dir") or "runtime",
        project_root=context.get("root") or ".",
    )


def strategy_create_activation_plan_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    return create_strategy_activation_plan(
        user_id=str(_context_value(args, context, "user_id", "default")),
        account_id=str(_context_value(args, context, "account_id", "")),
        strategy_id=str(args.get("strategy_id") or ""),
        strategy_version=str(args.get("strategy_version") or ""),
        effective_from=str(args.get("effective_from") or ""),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        run_id=str(_context_value(args, context, "run_id", "")),
        db_path=_db_path(context),
        output_dir=_output_dir(context),
    )


def strategy_create_binding_rollback_plan_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    return create_strategy_binding_rollback_plan(
        user_id=str(_context_value(args, context, "user_id", "default")),
        account_id=str(_context_value(args, context, "account_id", "")),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        run_id=str(_context_value(args, context, "run_id", "")),
        db_path=_db_path(context),
        output_dir=_output_dir(context),
    )


def strategy_binding_commit_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    return commit_strategy_binding_plan(
        user_id=str(_context_value(args, context, "user_id", "default")),
        plan_id=str(args.get("plan_id") or ""),
        confirmation_token=str(args.get("confirmation_token") or ""),
        conversation_id=str(
            _context_value(args, context, "conversation_id", _session_id(context))
        ),
        db_path=_db_path(context),
        output_dir=_output_dir(context),
    )


def strategy_preview_position_change_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    user_id = str(_context_value(args, context, "user_id", "default"))
    recommendations = args.get("recommendations")
    return preview_current_strategy_position_change(
        user_id=user_id,
        account_id=str(
            _context_value(
                args,
                context,
                "account_id",
                f"paper_{user_id}",
            )
        ),
        recommendations=(
            [dict(item or {}) for item in recommendations]
            if isinstance(recommendations, list)
            else None
        ),
        trade_date=str(args.get("trade_date") or ""),
        conversation_id=str(
            _context_value(
                args,
                context,
                "conversation_id",
                _session_id(context),
            )
        ),
        run_id=str(_context_value(args, context, "run_id", "")),
        db_path=_db_path(context),
        output_dir=_output_dir(context),
    )


def strategy_position_commit_adapter(
    args: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    return commit_current_strategy_position_change(
        user_id=str(
            _context_value(args, context, "user_id", "default")
        ),
        plan_id=str(args.get("plan_id") or ""),
        confirmation_token=str(args.get("confirmation_token") or ""),
        conversation_id=str(
            _context_value(
                args,
                context,
                "conversation_id",
                _session_id(context),
            )
        ),
        db_path=_db_path(context),
        output_dir=_output_dir(context),
    )


def strategy_disable_preview_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return write_operation_service.create_strategy_disable_proposal(
        user_id=str(_context_value(args, context, "user_id", "default")),
        strategy_id=str(args.get("strategy_id") or ""),
        version=str(args.get("strategy_version") or args.get("version") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )


def strategy_builder_preview_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return prepare_strategy_change(
        user_id=str(_context_value(args, context, "user_id", "default")),
        requirement=str(args.get("requirement") or context.get("query") or ""),
        parameters=args.get("parameters") if isinstance(args.get("parameters"), dict) else dict(args or {}),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )


def strategy_management_preview_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return manage_strategy(
        user_id=str(_context_value(args, context, "user_id", "default")),
        action=str(args.get("action") or "list"),
        strategy_id=str(args.get("strategy_id") or ""),
        version=str(args.get("strategy_version") or args.get("version") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )


def strategy_disable_commit_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return write_operation_service.commit_strategy_disable(
        user_id=str(_context_value(args, context, "user_id", "default")),
        plan_id=str(args.get("plan_id") or ""),
        confirmation_token=str(args.get("confirmation_token") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )


def capital_change_preview_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return write_operation_service.create_capital_change_proposal(
        user_id=str(_context_value(args, context, "user_id", "default")),
        flow_type=str(args.get("flow_type") or ""),
        amount=float(args.get("amount") or 0.0),
        effective_date=str(args.get("effective_date") or ""),
        reason=str(args.get("reason") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )


def capital_change_commit_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return write_operation_service.commit_capital_change(
        user_id=str(_context_value(args, context, "user_id", "default")),
        plan_id=str(args.get("plan_id") or ""),
        confirmation_token=str(args.get("confirmation_token") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )


def backfill_preview_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return write_operation_service.create_backfill_proposal(
        user_id=str(_context_value(args, context, "user_id", "default")),
        start_date=str(args.get("start_date") or ""),
        end_date=str(args.get("end_date") or "latest"),
        initial_cash=args.get("initial_cash"),
        resume=bool(args.get("resume", True)),
        force=bool(args.get("force", False)),
        skip_news=bool(args.get("skip_news", False)),
        strategy=str(args.get("strategy") or "hierarchical_top10"),
        top_k=int(args.get("top_k") or 15),
        entry_top_k=int(args.get("entry_top_k") or 10),
        hold_buffer_rank=int(args.get("hold_buffer_rank") or 15),
        max_positions=int(args.get("max_positions") or 10),
        continue_on_error=bool(args.get("continue_on_error", False)),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )


def backfill_commit_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return write_operation_service.commit_backfill(
        user_id=str(_context_value(args, context, "user_id", "default")),
        plan_id=str(args.get("plan_id") or ""),
        confirmation_token=str(args.get("confirmation_token") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )


def approval_confirm_plan_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return write_operation_service.confirm_existing_plan(
        user_id=str(_context_value(args, context, "user_id", "default")),
        plan_id=str(args.get("plan_id") or ""),
        confirmation_token=str(args.get("confirmation_token") or ""),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
        session_id=_session_id(context),
    )
