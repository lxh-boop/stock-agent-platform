from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.strategy_context_service import StrategyContextService
from agent_graph.services.strategy_proposal_service import StrategyProposalService
from agent_graph.services.strategy_implementation_service import (
    StrategyImplementationService,
)
from agent_graph.services.strategy_review_service import StrategyReviewService
from agent_graph.services.strategy_apply_service import StrategyApplyService
from agent_graph.services.strategy_binding_service import StrategyBindingService
from agent_graph.services.strategy_position_service import StrategyPositionService
from agent_graph.services.strategy_audit_service import StrategyAuditService
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult


IMPLEMENTATION_CONFIRMATION_QUESTION = "那现在需要我开始调整策略吗？"
CONVERSATION_ACTIONS = {
    "continue_discussion",
    "save_proposal",
    "ask_implementation",
    "prepare_implementation",
    "llm_unavailable",
}


def _scope(
    *,
    user_id: str,
    account_id: str,
    conversation_id: str,
) -> tuple[str, str, str]:
    scoped_user = str(user_id or "default")
    scoped_account = str(account_id or f"paper_{scoped_user}")
    return scoped_user, scoped_account, str(conversation_id or "")


def get_strategy_context(
    *,
    user_id: str,
    account_id: str = "",
    conversation_id: str = "",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
) -> ToolResult:
    scoped_user, scoped_account, scoped_conversation = _scope(
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
    )
    context = StrategyContextService(
        db_path=db_path,
        output_dir=output_dir,
    ).load(
        user_id=scoped_user,
        account_id=scoped_account,
        conversation_id=scoped_conversation,
    )
    return ToolResult(
        success=True,
        message="已读取长期策略讨论上下文。",
        data={"strategy_conversation_context": context.to_dict()},
        permission=ToolPermission.READ,
        tool_name="strategy.get_context",
    )


def get_active_strategy_proposal(
    *,
    user_id: str,
    account_id: str = "",
    conversation_id: str = "",
    db_path: str | Path | None = None,
) -> ToolResult:
    scoped_user, scoped_account, scoped_conversation = _scope(
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
    )
    service = StrategyProposalService(db_path)
    proposal = service.get_active(
        user_id=scoped_user,
        account_id=scoped_account,
        conversation_id=scoped_conversation,
    )
    versions = (
        service.list_versions(proposal.proposal_id, user_id=scoped_user)
        if proposal
        else []
    )
    return ToolResult(
        success=True,
        message="已读取当前会话的策略方案草案。",
        data={
            "proposal": proposal.to_dict() if proposal else {},
            "versions": [item.to_dict() for item in versions],
        },
        permission=ToolPermission.READ,
        tool_name="strategy.get_active_proposal",
    )


def save_strategy_proposal_draft(
    *,
    user_id: str,
    account_id: str = "",
    conversation_id: str = "",
    original_request: str = "",
    proposal_json: dict[str, Any] | None = None,
    user_feedback: str = "",
    change_summary: str = "",
    conversation_action: str = "save_proposal",
    proposal_id: str = "",
    base_strategy_id: str = "hierarchical_top10",
    base_strategy_version: str = "1.0.0",
    source_run_id: str = "",
    db_path: str | Path | None = None,
) -> ToolResult:
    """Persist the exact LLM proposal; never infer strategy semantics from text."""

    scoped_user, scoped_account, scoped_conversation = _scope(
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
    )
    action = str(conversation_action or "save_proposal")
    if action not in CONVERSATION_ACTIONS:
        return ToolResult(
            success=False,
            message="无效的策略对话动作。",
            errors=["invalid_strategy_conversation_action"],
            permission=ToolPermission.PREVIEW,
            tool_name="strategy.save_proposal_draft",
        )

    service = StrategyProposalService(db_path)
    active = service.get_active(
        user_id=scoped_user,
        account_id=scoped_account,
        conversation_id=scoped_conversation,
    )
    proposal = active
    version = None
    exact_proposal = dict(proposal_json or {})

    should_save = bool(exact_proposal) or action == "llm_unavailable"
    if should_save:
        if not exact_proposal:
            exact_proposal = {
                "original_request": str(original_request or user_feedback or ""),
                "llm_interpretation_required": True,
            }
        proposal, version = service.save_draft(
            user_id=scoped_user,
            account_id=scoped_account,
            conversation_id=scoped_conversation,
            original_request=str(original_request or ""),
            proposal_json=exact_proposal,
            base_strategy_id=str(base_strategy_id or ""),
            base_strategy_version=str(base_strategy_version or ""),
            user_feedback=str(user_feedback or ""),
            change_summary=str(change_summary or ""),
            source_run_id=str(source_run_id or ""),
            proposal_id=str(proposal_id or ""),
        )

    if action == "prepare_implementation":
        if proposal is None:
            return ToolResult(
                success=False,
                message="当前会话没有可锁定的策略方案草案。",
                errors=["active_strategy_proposal_required"],
                permission=ToolPermission.PREVIEW,
                tool_name="strategy.save_proposal_draft",
            )
        message = "已确认进入隔离实施准备；尚未修改正式项目或模拟盘状态。"
    elif action in {"ask_implementation", "llm_unavailable"}:
        message = IMPLEMENTATION_CONFIRMATION_QUESTION
    elif action == "continue_discussion":
        message = "已保留当前策略讨论上下文，尚未进入实施准备。"
    else:
        message = "已保存策略方案草案；尚未进入实施准备。"

    return ToolResult(
        success=True,
        message=message,
        data={
            "conversation_action": action,
            "proposal": proposal.to_dict() if proposal else {},
            "proposal_version": version.to_dict() if version else {},
            "implementation_requested": action == "prepare_implementation",
            "formal_write_created": False,
            "registry_changed": False,
            "binding_changed": False,
            "positions_changed": False,
        },
        permission=ToolPermission.PREVIEW,
        tool_name="strategy.save_proposal_draft",
        requires_confirmation=False,
    )


def prepare_strategy_implementation(
    *,
    proposal_id: str,
    proposal_version: int,
    user_id: str,
    account_id: str,
    conversation_id: str,
    run_id: str,
    db_path: str | Path | None = None,
    runtime_dir: str | Path = "runtime",
    project_root: str | Path = ".",
) -> ToolResult:
    """Lock one exact Proposal version and create isolated artifacts."""

    try:
        implementation = StrategyImplementationService(
            db_path=db_path,
            runtime_dir=runtime_dir,
        ).lock_and_prepare(
            proposal_id=str(proposal_id or ""),
            proposal_version=int(proposal_version),
            user_id=str(user_id or "default"),
            account_id=str(account_id or ""),
            conversation_id=str(conversation_id or ""),
            run_id=str(run_id or ""),
        )
        implementation = StrategyReviewService(
            db_path=db_path,
            runtime_dir=runtime_dir,
            project_root=project_root,
        ).validate_and_preview(
            implementation.implementation_id,
            user_id=str(user_id or "default"),
        )
    except (ValueError, PermissionError) as exc:
        return ToolResult(
            success=False,
            message="无法准备隔离策略实现。",
            errors=[str(exc)],
            permission=ToolPermission.PREVIEW,
            tool_name="strategy.prepare_implementation",
        )
    validation_passed = implementation.status == "validated"
    return ToolResult(
        success=validation_passed,
        message=(
            "已完成隔离策略实现、安全校验和回测，尚未修改正式项目或模拟盘状态。"
            if validation_passed
            else "隔离策略实现未通过校验；已安全保留报告并退回方案讨论。"
        ),
        data={
            **implementation.to_dict(),
            "formal_project_changed": False,
            "registry_changed": False,
            "account_changed": False,
            "positions_changed": False,
        },
        permission=ToolPermission.PREVIEW,
        tool_name="strategy.prepare_implementation",
        requires_confirmation=False,
        errors=[] if validation_passed else ["strategy_validation_failed"],
    )


def create_strategy_apply_plan(
    *,
    implementation_id: str,
    user_id: str,
    account_id: str,
    conversation_id: str,
    run_id: str,
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
    runtime_dir: str | Path = "runtime",
    project_root: str | Path = ".",
) -> ToolResult:
    return StrategyApplyService(
        db_path=db_path,
        output_dir=output_dir,
        runtime_dir=runtime_dir,
        project_root=project_root,
    ).create_plan(
        implementation_id=implementation_id,
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
        run_id=run_id,
    )


def commit_strategy_apply_plan(
    *,
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    conversation_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
    runtime_dir: str | Path = "runtime",
    project_root: str | Path = ".",
) -> ToolResult:
    return StrategyApplyService(
        db_path=db_path,
        output_dir=output_dir,
        runtime_dir=runtime_dir,
        project_root=project_root,
    ).commit(
        user_id=user_id,
        plan_id=plan_id,
        confirmation_token=confirmation_token,
        conversation_id=conversation_id,
    )


def create_strategy_activation_plan(
    *,
    user_id: str,
    account_id: str,
    strategy_id: str,
    strategy_version: str,
    effective_from: str = "",
    conversation_id: str = "",
    run_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> ToolResult:
    return StrategyBindingService(
        db_path=db_path,
        output_dir=output_dir,
    ).create_activation_plan(
        user_id=user_id,
        account_id=account_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        effective_from=effective_from,
        conversation_id=conversation_id,
        run_id=run_id,
    )


def create_strategy_binding_rollback_plan(
    *,
    user_id: str,
    account_id: str,
    conversation_id: str = "",
    run_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> ToolResult:
    return StrategyBindingService(
        db_path=db_path,
        output_dir=output_dir,
    ).create_rollback_plan(
        user_id=user_id,
        account_id=account_id,
        conversation_id=conversation_id,
        run_id=run_id,
    )


def commit_strategy_binding_plan(
    *,
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    conversation_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> ToolResult:
    return StrategyBindingService(
        db_path=db_path,
        output_dir=output_dir,
    ).commit(
        user_id=user_id,
        plan_id=plan_id,
        confirmation_token=confirmation_token,
        conversation_id=conversation_id,
    )


def preview_current_strategy_position_change(
    *,
    user_id: str,
    account_id: str = "",
    recommendations: list[dict[str, Any]] | None = None,
    trade_date: str = "",
    conversation_id: str = "",
    run_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> ToolResult:
    return StrategyPositionService(
        db_path=db_path,
        output_dir=output_dir,
    ).preview(
        user_id=user_id,
        account_id=account_id,
        recommendations=recommendations,
        trade_date=trade_date,
        conversation_id=conversation_id,
        run_id=run_id,
    )


def commit_current_strategy_position_change(
    *,
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    conversation_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> ToolResult:
    return StrategyPositionService(
        db_path=db_path,
        output_dir=output_dir,
    ).commit(
        user_id=user_id,
        plan_id=plan_id,
        confirmation_token=confirmation_token,
        conversation_id=conversation_id,
    )


def get_strategy_audit_trace(
    *,
    user_id: str,
    proposal_id: str = "",
    implementation_id: str = "",
    plan_id: str = "",
    commit_id: str = "",
    binding_id: str = "",
    run_id: str = "",
    conversation_id: str = "",
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> ToolResult:
    trace = StrategyAuditService(
        db_path=db_path,
        output_dir=output_dir,
    ).trace(
        user_id=user_id,
        proposal_id=proposal_id,
        implementation_id=implementation_id,
        plan_id=plan_id,
        commit_id=commit_id,
        binding_id=binding_id,
        run_id=run_id,
        conversation_id=conversation_id,
    )
    return ToolResult(
        success=True,
        message="已重建策略调整审计链。",
        data={"strategy_audit_trace": trace},
        permission=ToolPermission.READ,
        tool_name="strategy.get_audit_trace",
    )
