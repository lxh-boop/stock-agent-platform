from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.session.confirmation_manager import (
    create_confirmation_plan,
    mark_plan_executed,
    persist_action_commit,
    validate_confirmation,
)
from agent_graph.session.pending_action_store import update_pending_plan
from agent_graph.domain_tools._common import now_text, safe_float
from agent_graph.domain_tools.audit_tool import write_agent_action_log, write_agent_confirmation_log
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from pipelines.paper_backfill_pipeline import run_paper_trading_backfill
from portfolio.cash_flow import add_cash_flow, parse_date_text
from portfolio.storage import PortfolioStorage
from strategies.registry import get_strategy_registry


def _storage(user_id: str, output_dir: str | Path, db_path: str | Path | None) -> PortfolioStorage:
    return PortfolioStorage(db_path, output_dir=Path(output_dir) / "portfolio" / str(user_id))


def _account_snapshot(user_id: str, output_dir: str | Path, db_path: str | Path | None) -> dict[str, Any]:
    account = _storage(user_id, output_dir, db_path).load_account(f"paper_{user_id}")
    if account is None:
        return {"account_id": f"paper_{user_id}", "user_id": user_id, "account_found": False}
    data = account.to_dict()
    data["account_found"] = True
    return data


def _same_number(left: Any, right: Any, *, tolerance: float = 1e-6) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except Exception:
        return str(left or "") == str(right or "")


def _reject_plan(
    user_id: str,
    plan: dict[str, Any] | None,
    *,
    status: str,
    message: str,
    output_dir: str | Path,
    db_path: str | Path | None,
) -> None:
    if not plan:
        return
    plan_id = str(plan.get("plan_id") or "")
    if plan_id:
        try:
            update_pending_plan(
                user_id,
                plan_id,
                {"execution_status": "rejected", "rejected_reason": status},
                output_dir,
            )
        except Exception:
            pass
    persist_action_commit(
        plan,
        db_path=db_path,
        status="rejected",
        result_summary={"execution_status": "rejected", "plan_id": plan_id},
        error_type=status,
        error_message=message,
    )


class WriteOperationService:
    """Thin service facade for protected write operations.

    The service owns proposal/revalidate/commit orchestration for P0 write paths.
    Existing business implementations remain the execution engine behind commit.
    """

    def create_strategy_disable_proposal(
        self,
        user_id: str,
        strategy_id: str,
        version: str = "",
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        registry = get_strategy_registry(output_dir=output_dir, db_path=db_path)
        manifest = registry.get(strategy_id, version)
        if manifest is None:
            return ToolResult(
                success=False,
                message="Strategy version not found.",
                data={"strategy_id": strategy_id, "version": version},
                errors=["strategy_version_not_found"],
                permission=ToolPermission.PREVIEW,
                tool_name="strategy.disable.preview",
            )
        current_state = manifest.to_dict()
        payload = {
            "operation_type": "strategy_disable",
            "action": "disable_strategy",
            "strategy_id": manifest.strategy_id,
            "strategy_version": manifest.version,
            "current_strategy_state": current_state,
            "before_state_summary": current_state,
            "proposed_changes": [
                {
                    "type": "disable_strategy",
                    "strategy_id": manifest.strategy_id,
                    "version": manifest.version,
                }
            ],
            "after_state_preview": {
                **current_state,
                "status": "disabled",
                "enabled_for_paper_trading": False,
            },
            "impact_summary": [
                "No paper order is executed during preview.",
                "Future target generation will stop using this strategy after confirmation.",
            ],
            "warnings": [
                "Strategy disable requires confirmation and revalidation before commit.",
            ],
            "validation_results": {
                "strategy_exists": True,
                "currently_enabled_for_paper_trading": bool(manifest.enabled_for_paper_trading),
            },
        }
        plan = create_confirmation_plan(
            user_id,
            "disable_strategy",
            payload,
            output_dir=output_dir,
            db_path=db_path,
        )
        write_agent_confirmation_log(
            user_id,
            plan_id=str(plan["plan_id"]),
            confirmation_status="pending",
            expires_at=str(plan.get("expires_at") or ""),
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        data = {
            **payload,
            "plan_id": plan["plan_id"],
            "confirmation_token": plan["confirmation_token"],
            "expires_at": plan["expires_at"],
            "plan_hash": plan["plan_hash"],
            "created_at": plan["created_at"],
        }
        return ToolResult(
            success=True,
            message="Strategy disable proposal created. Confirmation is required.",
            data=data,
            warnings=list(payload["warnings"]),
            permission=ToolPermission.PREVIEW,
            tool_name="strategy.disable.preview",
            requires_confirmation=True,
            confirmation_token=str(plan["confirmation_token"]),
        )

    def commit_strategy_disable(
        self,
        user_id: str,
        plan_id: str,
        confirmation_token: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        ok, status, plan = validate_confirmation(
            user_id,
            plan_id,
            confirmation_token,
            output_dir=output_dir,
            db_path=db_path,
        )
        if not ok or not plan:
            return ToolResult(False, f"Confirmation rejected: {status}", data={"plan_id": plan_id}, errors=[status], permission=ToolPermission.WRITE, tool_name="strategy.disable.commit")
        if str(plan.get("intent") or "") != "disable_strategy":
            message = "Unsupported confirmation plan intent."
            _reject_plan(user_id, plan, status="unsupported_plan_intent", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id, "intent": plan.get("intent")}, errors=["unsupported_plan_intent"], permission=ToolPermission.WRITE, tool_name="strategy.disable.commit")

        registry = get_strategy_registry(output_dir=output_dir, db_path=db_path)
        strategy_id = str(plan.get("strategy_id") or "")
        version = str(plan.get("strategy_version") or "")
        manifest = registry.get(strategy_id, version)
        if manifest is None:
            message = "Strategy version not found during revalidation."
            _reject_plan(user_id, plan, status="strategy_version_not_found", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id, "strategy_id": strategy_id, "version": version}, errors=["strategy_version_not_found"], permission=ToolPermission.WRITE, tool_name="strategy.disable.commit")
        before = dict(plan.get("current_strategy_state") or plan.get("before_state_summary") or {})
        if (
            str(before.get("status") or "") != str(manifest.status or "")
            or bool(before.get("enabled_for_paper_trading")) != bool(manifest.enabled_for_paper_trading)
            or str(before.get("code_hash") or "") != str(manifest.code_hash or "")
        ):
            message = "Strategy state changed after preview."
            _reject_plan(user_id, plan, status="business_state_changed", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id}, errors=["business_state_changed"], permission=ToolPermission.WRITE, tool_name="strategy.disable.commit")
        if not manifest.enabled_for_paper_trading and manifest.status == "disabled":
            message = "Strategy is already disabled."
            _reject_plan(user_id, plan, status="strategy_already_disabled", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id}, errors=["strategy_already_disabled"], permission=ToolPermission.WRITE, tool_name="strategy.disable.commit")

        disabled = registry.disable(strategy_id, version)
        mark_plan_executed(user_id, plan_id, output_dir=output_dir, db_path=db_path, strategy_status="disabled")
        write_agent_confirmation_log(
            user_id,
            plan_id=plan_id,
            confirmation_status="confirmed",
            expires_at=str(plan.get("expires_at") or ""),
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        write_agent_action_log(
            user_id,
            intent="disable_strategy",
            tool_name="strategy.disable.commit",
            tool_input={"plan_id": plan_id},
            tool_output_summary=disabled.to_dict(),
            plan_id=plan_id,
            confirmation_status="confirmed",
            execution_status="executed",
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        return ToolResult(
            True,
            "Strategy disabled after confirmation.",
            data={"plan_id": plan_id, "strategy_manifest": disabled.to_dict()},
            permission=ToolPermission.WRITE,
            tool_name="strategy.disable.commit",
        )

    def create_capital_change_proposal(
        self,
        user_id: str,
        flow_type: str,
        amount: float,
        effective_date: str,
        *,
        reason: str = "",
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        flow_type = str(flow_type or "").lower().strip()
        amount_value = safe_float(amount, 0.0)
        if flow_type not in {"deposit", "withdrawal"}:
            return ToolResult(False, "Invalid flow_type.", errors=["invalid_flow_type"], permission=ToolPermission.PREVIEW, tool_name="capital.change.preview")
        if amount_value <= 0:
            return ToolResult(False, "Amount must be greater than 0.", errors=["invalid_amount"], permission=ToolPermission.PREVIEW, tool_name="capital.change.preview")
        effective = parse_date_text(effective_date or now_text())
        account = _account_snapshot(user_id, output_dir, db_path)
        before_cash = safe_float(account.get("cash"), 0.0)
        delta = amount_value if flow_type == "deposit" else -amount_value
        payload = {
            "operation_type": "cash_flow",
            "flow_type": flow_type,
            "amount": amount_value,
            "effective_date": effective,
            "reason": reason or "agent capital change",
            "before_state_summary": {
                "account_id": account.get("account_id"),
                "user_id": user_id,
                "cash": before_cash,
                "total_assets": safe_float(account.get("total_assets"), before_cash),
                "account_found": bool(account.get("account_found")),
            },
            "proposed_changes": [
                {
                    "type": "cash_flow",
                    "direction": flow_type,
                    "amount": amount_value,
                    "effective_date": effective,
                    "reason": reason or "agent capital change",
                }
            ],
            "after_state_preview": {
                "expected_after_cash": before_cash + delta,
                "pending_flow_status": "pending",
            },
            "validation_results": {
                "amount_positive": True,
                "flow_type_valid": True,
            },
            "warnings": [
                "Capital change is saved as a pending cash flow and may require replay/backfill to affect history.",
            ],
        }
        plan = create_confirmation_plan(user_id, "capital_change", payload, output_dir=output_dir, db_path=db_path)
        write_agent_confirmation_log(user_id, plan_id=plan["plan_id"], confirmation_status="pending", expires_at=plan["expires_at"], session_id=session_id, output_dir=output_dir, db_path=db_path)
        write_agent_action_log(user_id, intent="preview_capital_change", tool_name="capital.change.preview", tool_input=payload, tool_output_summary={"plan_id": plan["plan_id"]}, plan_id=plan["plan_id"], confirmation_status="pending", execution_status="preview_only", trade_date=effective, session_id=session_id, output_dir=output_dir, db_path=db_path)
        return ToolResult(
            True,
            "Capital change proposal created. Confirmation is required.",
            data={"plan_id": plan["plan_id"], "confirmation_token": plan["confirmation_token"], "expires_at": plan["expires_at"], "plan_hash": plan["plan_hash"], "created_at": plan["created_at"], **payload},
            warnings=list(payload["warnings"]),
            permission=ToolPermission.PREVIEW,
            tool_name="capital.change.preview",
            requires_confirmation=True,
            confirmation_token=str(plan["confirmation_token"]),
        )

    def commit_capital_change(
        self,
        user_id: str,
        plan_id: str,
        confirmation_token: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        ok, status, plan = validate_confirmation(user_id, plan_id, confirmation_token, output_dir=output_dir, db_path=db_path)
        if not ok or not plan:
            return ToolResult(False, f"Confirmation rejected: {status}", data={"plan_id": plan_id}, errors=[status], permission=ToolPermission.WRITE, tool_name="capital.change.commit")
        if str(plan.get("intent") or "") != "capital_change":
            message = "Unsupported confirmation plan intent."
            _reject_plan(user_id, plan, status="unsupported_plan_intent", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id, "intent": plan.get("intent")}, errors=["unsupported_plan_intent"], permission=ToolPermission.WRITE, tool_name="capital.change.commit")
        amount = safe_float(plan.get("amount"), 0.0)
        if amount <= 0:
            message = "Invalid capital change amount."
            _reject_plan(user_id, plan, status="invalid_amount", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id}, errors=["invalid_amount"], permission=ToolPermission.WRITE, tool_name="capital.change.commit")
        before = dict(plan.get("before_state_summary") or {})
        account = _account_snapshot(user_id, output_dir, db_path)
        if before.get("account_found") and account.get("account_found") and not _same_number(before.get("cash"), account.get("cash")):
            message = "Account cash changed after preview."
            _reject_plan(user_id, plan, status="business_state_changed", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id}, errors=["business_state_changed"], permission=ToolPermission.WRITE, tool_name="capital.change.commit")
        flow = add_cash_flow(
            user_id=user_id,
            flow_type=str(plan.get("flow_type")),
            amount=amount,
            effective_date=str(plan.get("effective_date")),
            reason=str(plan.get("reason") or "agent capital change"),
            source="app",
            run_id=str(plan_id),
            idempotency_key=str(plan_id),
            db_path=db_path,
            output_dir=output_dir,
            use_database=True,
        )
        mark_plan_executed(user_id, plan_id, output_dir=output_dir, db_path=db_path, cash_flow_id=flow.cash_flow_id)
        write_agent_confirmation_log(user_id, plan_id=plan_id, confirmation_status="confirmed", expires_at=str(plan.get("expires_at") or ""), session_id=session_id, output_dir=output_dir, db_path=db_path)
        write_agent_action_log(user_id, intent="execute_capital_change", tool_name="capital.change.commit", tool_input={"plan_id": plan_id}, tool_output_summary=flow.to_dict(), plan_id=plan_id, confirmation_status="confirmed", execution_status="executed", trade_date=flow.effective_date, session_id=session_id, output_dir=output_dir, db_path=db_path)
        return ToolResult(True, "Capital flow saved. Scheduled/backfill jobs apply pending flows by effective date.", data={"cash_flow": flow.to_dict(), "plan_id": plan_id}, permission=ToolPermission.WRITE, tool_name="capital.change.commit")

    def create_backfill_proposal(
        self,
        user_id: str,
        start_date: str,
        *,
        end_date: str = "latest",
        initial_cash: float | None = None,
        resume: bool = True,
        force: bool = False,
        skip_news: bool = False,
        strategy: str = "hierarchical_top10",
        top_k: int = 15,
        entry_top_k: int = 10,
        hold_buffer_rank: int = 15,
        max_positions: int = 10,
        continue_on_error: bool = False,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        account = _account_snapshot(user_id, output_dir, db_path)
        positions = _storage(user_id, output_dir, db_path).load_positions(user_id)
        payload = {
            "operation_type": "paper_backfill",
            "start_date": start_date,
            "end_date": end_date,
            "initial_cash": initial_cash,
            "resume": bool(resume),
            "force": bool(force),
            "skip_news": bool(skip_news),
            "strategy": str(strategy or "hierarchical_top10"),
            "top_k": int(top_k or 15),
            "entry_top_k": int(entry_top_k or 10),
            "hold_buffer_rank": int(hold_buffer_rank or 15),
            "max_positions": int(max_positions or 10),
            "continue_on_error": bool(continue_on_error),
            "before_state_summary": {
                "account": {
                    "account_id": account.get("account_id"),
                    "cash": safe_float(account.get("cash"), 0.0),
                    "total_assets": safe_float(account.get("total_assets"), 0.0),
                    "account_found": bool(account.get("account_found")),
                },
                "position_count": len(positions),
            },
            "proposed_changes": [
                {
                    "type": "paper_backfill",
                    "date_range": {"start_date": start_date, "end_date": end_date},
                    "force": bool(force),
                    "resume": bool(resume),
                    "affects": ["orders", "positions", "account", "nav_history"],
                }
            ],
            "after_state_preview": {
                "will_rebuild_history": bool(force),
                "will_resume_existing_state": bool(resume) and not bool(force),
            },
            "validation_results": {
                "requires_confirmation": True,
            },
            "warnings": [
                "Backfill may rewrite paper-trading orders, positions, cash and NAV history.",
            ],
        }
        plan = create_confirmation_plan(user_id, "paper_backfill", payload, output_dir=output_dir, db_path=db_path)
        write_agent_confirmation_log(user_id, plan["plan_id"], "pending", expires_at=plan["expires_at"], session_id=session_id, output_dir=output_dir, db_path=db_path)
        write_agent_action_log(user_id, intent="preview_backfill", tool_name="backfill.preview", tool_input=payload, tool_output_summary={"plan_id": plan["plan_id"]}, plan_id=plan["plan_id"], confirmation_status="pending", execution_status="preview_only", session_id=session_id, output_dir=output_dir, db_path=db_path)
        return ToolResult(
            True,
            "Backfill proposal created. Confirmation is required because it rewrites paper-trading state.",
            data={"plan_id": plan["plan_id"], "confirmation_token": plan["confirmation_token"], "expires_at": plan["expires_at"], "plan_hash": plan["plan_hash"], "created_at": plan["created_at"], **payload},
            warnings=list(payload["warnings"]),
            permission=ToolPermission.PREVIEW,
            tool_name="backfill.preview",
            requires_confirmation=True,
            confirmation_token=str(plan["confirmation_token"]),
        )

    def commit_backfill(
        self,
        user_id: str,
        plan_id: str,
        confirmation_token: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        ok, status, plan = validate_confirmation(user_id, plan_id, confirmation_token, output_dir=output_dir, db_path=db_path)
        if not ok or not plan:
            return ToolResult(False, f"Confirmation rejected: {status}", data={"plan_id": plan_id}, errors=[status], permission=ToolPermission.WRITE, tool_name="backfill.commit")
        if str(plan.get("intent") or "") != "paper_backfill":
            message = "Unsupported confirmation plan intent."
            _reject_plan(user_id, plan, status="unsupported_plan_intent", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id, "intent": plan.get("intent")}, errors=["unsupported_plan_intent"], permission=ToolPermission.WRITE, tool_name="backfill.commit")
        before = dict(plan.get("before_state_summary") or {})
        before_account = dict(before.get("account") or {})
        account = _account_snapshot(user_id, output_dir, db_path)
        if before_account.get("account_found") and account.get("account_found") and not _same_number(before_account.get("cash"), account.get("cash")):
            message = "Account cash changed after backfill preview."
            _reject_plan(user_id, plan, status="business_state_changed", message=message, output_dir=output_dir, db_path=db_path)
            return ToolResult(False, message, data={"plan_id": plan_id}, errors=["business_state_changed"], permission=ToolPermission.WRITE, tool_name="backfill.commit")
        result = run_paper_trading_backfill(
            user_id=user_id,
            start_date=str(plan.get("start_date")),
            end_date=str(plan.get("end_date") or "latest"),
            initial_cash=plan.get("initial_cash"),
            resume=bool(plan.get("resume", True)),
            force=bool(plan.get("force", False)),
            dry_run=False,
            skip_news=bool(plan.get("skip_news", False)),
            top_k=int(plan.get("top_k") or 15),
            strategy=str(plan.get("strategy") or "hierarchical_top10"),
            entry_top_k=int(plan.get("entry_top_k") or 10),
            hold_buffer_rank=int(plan.get("hold_buffer_rank") or 15),
            max_positions=int(plan.get("max_positions") or 10),
            continue_on_error=bool(plan.get("continue_on_error", False)),
            output_dir=output_dir,
            db_path=db_path,
        )
        mark_plan_executed(user_id, plan_id, output_dir=output_dir, db_path=db_path, backfill_status=result.status)
        write_agent_action_log(user_id, intent="execute_backfill", tool_name="backfill.commit", tool_input={"plan_id": plan_id}, tool_output_summary=result.to_dict(), plan_id=plan_id, confirmation_status="confirmed", execution_status="executed", trade_date=result.end_date, session_id=session_id, output_dir=output_dir, db_path=db_path)
        return ToolResult(True, "Backfill completed.", data=result.to_dict(), permission=ToolPermission.WRITE, tool_name="backfill.commit")

    def confirm_existing_plan(
        self,
        user_id: str,
        plan_id: str,
        confirmation_token: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        from agent_graph.session.pending_action_store import get_pending_plan

        plan = get_pending_plan(user_id, plan_id, output_dir)
        if not plan:
            return ToolResult(False, "Pending plan not found.", data={"plan_id": plan_id}, errors=["plan_not_found"], permission=ToolPermission.WRITE, tool_name="approval.confirm_plan")
        intent = str(plan.get("intent") or "")
        if intent == "capital_change":
            return self.commit_capital_change(user_id, plan_id, confirmation_token, output_dir=output_dir, db_path=db_path, session_id=session_id)
        if intent == "paper_backfill":
            return self.commit_backfill(user_id, plan_id, confirmation_token, output_dir=output_dir, db_path=db_path, session_id=session_id)
        if intent == "disable_strategy":
            return self.commit_strategy_disable(user_id, plan_id, confirmation_token, output_dir=output_dir, db_path=db_path, session_id=session_id)
        if intent in {"execute_add_stock", "execute_adjust_position"}:
            from agent_graph.services.portfolio_proposal_service import portfolio_proposal_service

            return portfolio_proposal_service.commit_paper_trade(user_id, plan_id, confirmation_token, output_dir=output_dir, db_path=db_path, session_id=session_id)
        if intent == "apply_strategy_implementation":
            from agent_graph.domain_tools.strategy_workflow_tools import (
                commit_strategy_apply_plan,
            )

            return commit_strategy_apply_plan(
                user_id=user_id,
                plan_id=plan_id,
                confirmation_token=confirmation_token,
                conversation_id=session_id,
                output_dir=output_dir,
                db_path=db_path,
                runtime_dir=Path(output_dir).parent / "runtime",
                project_root=".",
            )
        if intent in {
            "activate_strategy_binding",
            "rollback_strategy_binding",
        }:
            from agent_graph.domain_tools.strategy_workflow_tools import (
                commit_strategy_binding_plan,
            )

            return commit_strategy_binding_plan(
                user_id=user_id,
                plan_id=plan_id,
                confirmation_token=confirmation_token,
                conversation_id=session_id,
                output_dir=output_dir,
                db_path=db_path,
            )
        if intent == "execute_strategy_position_change":
            from agent_graph.domain_tools.strategy_workflow_tools import (
                commit_current_strategy_position_change,
            )

            return commit_current_strategy_position_change(
                user_id=user_id,
                plan_id=plan_id,
                confirmation_token=confirmation_token,
                conversation_id=session_id,
                output_dir=output_dir,
                db_path=db_path,
            )
        if intent in {"register_strategy", "enable_strategy"}:
            from agent_graph.domain_tools.strategy_management_tool import execute_confirmed_strategy_plan

            return execute_confirmed_strategy_plan(user_id, plan_id, confirmation_token, output_dir=output_dir, db_path=db_path, session_id=session_id)
        return ToolResult(False, "Unsupported confirmation plan intent.", data={"plan_id": plan_id, "intent": intent}, errors=["unsupported_plan_intent"], permission=ToolPermission.WRITE, tool_name="approval.confirm_plan")


write_operation_service = WriteOperationService()
