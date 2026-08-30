from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

from agent.session.confirmation_manager import (
    inspect_confirmation,
    mark_plan_executed,
    mark_plan_revalidation_failed,
    validate_confirmation,
)
from agent.tools._common import is_valid_agent_price, now_text, safe_float
from agent.tools.audit_tool import write_agent_action_log, write_agent_confirmation_log
from agent.tools.portfolio_state_tool import query_portfolio_state
from agent.tools.tool_schemas import PaperTradeExecutionResult, ToolPermission, ToolResult
from pipelines.paper_trading_pipeline import run_paper_trading_pipeline
from pipelines.schemas import PipelineContext, PipelineStatus
from portfolio.paper_trading_engine import execute_paper_rebalance
from portfolio.performance_metrics import build_nav_record
from portfolio.portfolio_risk import calculate_portfolio_risk
from portfolio.schemas import RebalanceDecision, RebalancePlan
from portfolio.storage import PortfolioStorage
from portfolio.user_profile import build_user_constraints, default_user_profile, load_user_context
from scheduler.trading_calendar import is_trading_day


def _stock_code(value: Any) -> str:
    return str(value or "").split(".")[0].zfill(6)


def _record_rejected_commit(
    plan: dict[str, Any] | None,
    *,
    db_path: str | Path | None,
    error_type: str,
    message: str,
    result_summary: dict[str, Any] | None = None,
) -> None:
    # A failed preflight/revalidation is not a commit. Failure details belong
    # to proposal/approval/runtime traces; action_commits is reserved for an
    # actual idempotent business-state commit.
    _ = (plan, db_path, error_type, message, result_summary)


def _decision_time_token(value: str) -> str:
    return str(value or now_text()).replace("-", "").replace(":", "").replace(" ", "_")


def _current_weight(position: Any, total_assets: float) -> float:
    weight = safe_float(getattr(position, "position_ratio", 0.0), 0.0)
    if weight <= 0 and total_assets > 0:
        weight = safe_float(getattr(position, "market_value", 0.0), 0.0) / total_assets
    return weight


def _adjust_decision_record(
    *,
    user_id: str,
    plan_id: str,
    trade_date: str,
    decision_time: str,
    decision: RebalanceDecision,
    order: Any | None,
) -> dict[str, Any]:
    quantity = safe_float(getattr(order, "quantity", 0.0), 0.0) if order else 0.0
    price = safe_float(getattr(order, "executed_price", decision.current_price), decision.current_price) if order else decision.current_price
    order_amount = safe_float(getattr(order, "order_amount", price * quantity), price * quantity) if order else 0.0
    total_fee = safe_float(getattr(order, "total_fee", 0.0), 0.0) if order else 0.0
    net_cash_change = safe_float(getattr(order, "net_cash_change", 0.0), 0.0) if order else 0.0
    paper_action = str(getattr(order, "paper_action", "") or f"paper_{decision.action}")
    return {
        "decision_id": decision.source_decision_id or f"paper_decision_{user_id}_{trade_date}_{decision.stock_code}",
        "user_id": user_id,
        "trade_date": trade_date,
        "decision_time": decision_time,
        "stock_code": decision.stock_code,
        "stock_name": decision.stock_name,
        "paper_action": paper_action,
        "target_weight": float(decision.target_weight or 0.0),
        "current_weight": float(decision.current_weight or 0.0),
        "order_amount": order_amount,
        "order_quantity": quantity,
        "executed_price": price,
        "total_fee": total_fee,
        "net_cash_change": net_cash_change,
        "original_rank": 0,
        "original_score": 0.0,
        "news_adjustment": 0.0,
        "user_adjustment": 0.0,
        "effective_news_adjustment": 0.0,
        "combined_adjustment": 0.0,
        "position_adjustment_ratio": float(decision.position_adjustment_ratio or 1.0),
        "reason": decision.reason,
        "risk_warning": decision.risk_warning,
        "triggered_rules": decision.triggered_rules,
        "source_decision_id": decision.source_decision_id,
        "job_id": "",
        "run_id": plan_id,
        "execution_source": "agent_confirmed_adjust_position",
        "created_at": now_text(),
    }


def _execute_adjust_position_plan(
    user_id: str,
    plan_id: str,
    plan: dict[str, Any],
    *,
    output_dir: str | Path,
    db_path: str | Path | None,
    session_id: str,
    canonical_approval: bool = False,
) -> ToolResult:
    code = _stock_code(plan.get("stock_code"))
    trade_date = str(plan.get("trade_date") or "")[:10]
    price = safe_float(plan.get("current_price"), 0.0)
    estimated_quantity = safe_float(plan.get("estimated_quantity") or plan.get("estimated_trade_quantity"), 0.0)
    if not is_valid_agent_price(price) or estimated_quantity <= 0 or estimated_quantity % 100 != 0:
        message = "Invalid price or A-share lot quantity; paper adjustment was rejected."
        _record_rejected_commit(
            plan,
            db_path=db_path,
            error_type="invalid_price_or_quantity",
            message=message,
            result_summary={"plan_id": plan_id, "price": price, "quantity": estimated_quantity},
        )
        return ToolResult(
            success=False,
            message=message,
            data={"plan_id": plan_id, "price": price, "quantity": estimated_quantity},
            errors=["invalid_price_or_quantity"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )

    storage = PortfolioStorage(
        db_path,
        output_dir=Path(output_dir) / "portfolio" / str(user_id),
        use_database=True,
    )
    account = storage.load_account(f"paper_{user_id}")
    positions = storage.load_positions(user_id)
    if account is None:
        message = "Paper account is missing; adjustment was not executed."
        _record_rejected_commit(
            plan,
            db_path=db_path,
            error_type="missing_paper_account",
            message=message,
            result_summary={"plan_id": plan_id},
        )
        return ToolResult(
            success=False,
            message=message,
            data={"plan_id": plan_id},
            errors=["missing_paper_account"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )

    current = next((item for item in positions if _stock_code(item.stock_code) == code and item.quantity > 0), None)
    if current is None:
        message = "The paper portfolio no longer holds this stock; regenerate the adjustment preview."
        _record_rejected_commit(
            plan,
            db_path=db_path,
            error_type="missing_position",
            message=message,
            result_summary={"plan_id": plan_id, "stock_code": code},
        )
        return ToolResult(
            success=False,
            message=message,
            data={"plan_id": plan_id, "stock_code": code},
            errors=["missing_position"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )

    total_assets = safe_float(account.total_assets or account.initial_cash, 0.0)
    current_weight = _current_weight(current, total_assets)
    target_weight = safe_float(plan.get("target_weight") or plan.get("recommended_weight"), 0.0)
    target_weight = max(0.0, min(1.0, target_weight))
    action = "sell" if target_weight <= 0 else "reduce" if target_weight < current_weight else "buy"
    decision = RebalanceDecision(
        stock_code=code,
        stock_name=str(plan.get("stock_name") or current.stock_name or code),
        action=action,
        target_weight=target_weight,
        reason="Agent confirmed explicit paper position adjustment.",
        risk_warning=str(plan.get("risk_warning") or ""),
        current_price=price,
        executable_quantity=safe_float(plan.get("target_quantity"), 0.0),
        current_weight=current_weight,
        source_decision_id=str((plan.get("recommendation_record") or {}).get("decision_id") or f"agent_adjust_{plan_id}_{code}"),
        position_adjustment_ratio=target_weight / current_weight if current_weight > 0 else 0.0,
    )
    rebalance_plan = RebalancePlan(
        user_id=user_id,
        trade_date=trade_date,
        decisions=[decision],
        total_target_weight=target_weight,
        run_id=str(plan_id),
        execution_source="agent_confirmed_adjust_position",
    )
    decision_time = now_text()
    try:
        stored_settings = storage.load_trading_settings(user_id)
    except Exception:
        stored_settings = None
    try:
        _, _, _, constraints = load_user_context(user_id, db_path=db_path, output_dir=output_dir)
    except Exception:
        constraints = build_user_constraints(default_user_profile(user_id))

    previous_total_assets = safe_float(account.total_assets or account.initial_cash, 0.0)
    previous_twr = safe_float(account.time_weighted_return, 0.0)
    nav_history = storage.load_nav_history(user_id)
    previous_nav_peak = max(
        [
            safe_float(row.get("nav_peak") or row.get("composite_nav") or row.get("nav"), 1.0)
            for row in nav_history
        ]
        or [1.0]
    )
    executed = execute_paper_rebalance(
        account=account,
        positions=positions,
        plan=rebalance_plan,
        price_lookup={code: price},
        decision_time=decision_time,
        cost_config=stored_settings,
        mark_price_lookup={code: price},
        persist=True,
        storage=storage,
        trading_permissions=constraints.get("trading_permissions"),
    )
    account_after = executed["account"]
    positions_after = executed["positions"]
    orders = executed["orders"]
    order_ids = [str(order.order_id) for order in orders]
    execution_status = "executed" if order_ids else "executed_no_order"

    nav_record = build_nav_record(
        account_after,
        trade_date,
        positions_after,
        previous_total_assets=previous_total_assets,
        previous_twr=previous_twr,
        previous_nav_peak=previous_nav_peak,
        daily_fee=safe_float(account_after.daily_fee, 0.0),
    )
    storage.save_nav_record(nav_record)
    risk_report = calculate_portfolio_risk(user_id, account_after, positions_after, constraints)
    storage.save_risk_report(risk_report)
    decisions = [
        _adjust_decision_record(
            user_id=user_id,
            plan_id=plan_id,
            trade_date=trade_date,
            decision_time=decision_time,
            decision=decision,
            order=orders[0] if orders else None,
        )
    ]
    snapshot_paths = storage.write_daily_snapshot(
        account=account_after,
        positions=positions_after,
        orders=orders,
        risk_report=risk_report,
        decisions=decisions,
        trade_date=trade_date,
        decision_time=_decision_time_token(decision_time),
    )
    if not canonical_approval:
        mark_plan_executed(
            user_id,
            plan_id,
            output_dir=output_dir,
            db_path=db_path,
            order_ids=order_ids,
            execution_status=execution_status,
        )
    write_agent_confirmation_log(
        user_id,
        plan_id=plan_id,
        confirmation_status="canonical_approved" if canonical_approval else "confirmed",
        expires_at=str(plan.get("expires_at") or ""),
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    write_agent_action_log(
        user_id,
        intent="execute_adjust_position",
        tool_name="paper_trade_execute",
        tool_input={"plan_id": plan_id},
        tool_output_summary={"order_ids": order_ids, "execution_status": execution_status},
        plan_id=plan_id,
        confirmation_status="canonical_approved" if canonical_approval else "confirmed",
        execution_status=execution_status,
        trade_date=trade_date,
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    output_paths = {
        "paper_account": str(storage.account_path),
        "paper_account_latest": str(storage.account_latest_path),
        "paper_positions": str(storage.positions_path),
        "paper_positions_latest": str(storage.positions_latest_path),
        "paper_orders": str(storage.orders_path),
        "paper_orders_latest": str(storage.orders_latest_path),
        "portfolio_risk_report": str(storage.risk_report_path),
        "portfolio_risk_report_latest": str(storage.risk_report_latest_path),
        "paper_nav_latest": str(storage.nav_latest_path),
        **snapshot_paths,
    }
    result = PaperTradeExecutionResult(
        success=bool(order_ids),
        plan_id=str(plan_id),
        confirmation_status="confirmed",
        execution_status=execution_status,
        order_ids=order_ids,
        position_count_after=len(positions_after),
        message=f"Executed {len(order_ids)} paper adjustment orders." if order_ids else "Plan executed but no nonzero paper order was produced.",
        output_paths=output_paths,
        warnings=[],
        errors=[] if order_ids else ["no_order_generated"],
    )
    return ToolResult(
        success=result.success,
        message=result.message,
        data=result.to_dict(),
        warnings=result.warnings,
        errors=result.errors,
        permission=ToolPermission.WRITE,
        tool_name="paper_trade_execute",
    )


def _execute_portfolio_rebalance_plan(
    user_id: str,
    plan_id: str,
    plan: dict[str, Any],
    *,
    output_dir: str | Path,
    db_path: str | Path | None,
    session_id: str,
    canonical_approval: bool = False,
) -> ToolResult:
    target_rows = [dict(item or {}) for item in (plan.get("target_positions") or [])]
    changed_rows = [
        row
        for row in target_rows
        if abs(safe_float(row.get("target_quantity"), 0.0) - safe_float(row.get("current_quantity"), 0.0)) > 1e-9
    ]
    if not changed_rows:
        return ToolResult(
            success=False,
            message="The portfolio proposal contains no executable position changes.",
            data={"plan_id": plan_id},
            errors=["no_executable_portfolio_changes"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )

    storage = PortfolioStorage(
        db_path,
        output_dir=Path(output_dir) / "portfolio" / str(user_id),
        use_database=True,
    )
    account = storage.load_account(f"paper_{user_id}")
    positions = storage.load_positions(user_id)
    if account is None:
        return ToolResult(
            success=False,
            message="Paper account is missing; portfolio rebalance was not executed.",
            data={"plan_id": plan_id},
            errors=["missing_paper_account"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )

    position_map = {_stock_code(item.stock_code): item for item in positions if item.quantity > 0}
    decisions: list[RebalanceDecision] = []
    price_lookup: dict[str, float] = {}
    for row in changed_rows:
        code = _stock_code(row.get("stock_code"))
        current = position_map.get(code)
        target_quantity = safe_float(row.get("target_quantity"), -1.0)
        current_quantity = safe_float(row.get("current_quantity"), -1.0)
        price = safe_float(row.get("current_price"), 0.0)
        if (
            current is None
            or target_quantity < 0
            or target_quantity % 100 != 0
            or abs(float(current.quantity) - current_quantity) > 1e-6
            or not is_valid_agent_price(price)
        ):
            return ToolResult(
                success=False,
                message="Portfolio target rows failed the one-lot/current-position preflight.",
                data={"plan_id": plan_id, "stock_code": code},
                errors=["portfolio_target_preflight_failed"],
                permission=ToolPermission.WRITE,
                tool_name="paper_trade_execute",
            )
        current_weight = _current_weight(current, safe_float(account.total_assets, 0.0))
        target_weight = safe_float(row.get("target_weight"), 0.0)
        action = "sell" if target_quantity <= 0 else "reduce" if target_quantity < current_quantity else "buy"
        decisions.append(
            RebalanceDecision(
                stock_code=code,
                stock_name=str(row.get("stock_name") or current.stock_name or code),
                action=action,
                target_weight=target_weight,
                reason=str(row.get("reason") or "Agent confirmed portfolio-level stability rebalance."),
                risk_warning="",
                current_price=price,
                executable_quantity=target_quantity,
                current_weight=current_weight,
                source_decision_id=f"agent_portfolio_{plan_id}_{code}",
                position_adjustment_ratio=target_weight / current_weight if current_weight > 0 else 0.0,
                industry=str(row.get("industry") or current.industry or ""),
            )
        )
        price_lookup[code] = price

    trade_date = str(plan.get("trade_date") or "")[:10]
    rebalance_plan = RebalancePlan(
        user_id=user_id,
        trade_date=trade_date,
        decisions=decisions,
        total_target_weight=sum(max(0.0, float(item.target_weight or 0.0)) for item in decisions),
        run_id=str(plan_id),
        execution_source="agent_confirmed_portfolio_rebalance",
    )
    try:
        stored_settings = storage.load_trading_settings(user_id)
    except Exception:
        stored_settings = None
    try:
        _, _, _, constraints = load_user_context(user_id, db_path=db_path, output_dir=output_dir)
    except Exception:
        constraints = build_user_constraints(default_user_profile(user_id))

    previous_total_assets = safe_float(account.total_assets or account.initial_cash, 0.0)
    previous_twr = safe_float(account.time_weighted_return, 0.0)
    previous_nav_peak = max(
        [safe_float(row.get("nav_peak") or row.get("composite_nav") or row.get("nav"), 1.0) for row in storage.load_nav_history(user_id)]
        or [1.0]
    )
    decision_time = now_text()
    portfolio_dir = storage.output_dir.resolve()

    with tempfile.TemporaryDirectory(prefix="stock_daily_portfolio_commit_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        files_backup = temp_dir / "portfolio_files"
        portfolio_existed = portfolio_dir.exists()
        if portfolio_existed:
            shutil.copytree(portfolio_dir, files_backup)
        try:
            executed = execute_paper_rebalance(
                account=account,
                positions=positions,
                plan=rebalance_plan,
                price_lookup=price_lookup,
                decision_time=decision_time,
                cost_config=stored_settings,
                mark_price_lookup=price_lookup,
                persist=True,
                storage=storage,
                trading_permissions=constraints.get("trading_permissions"),
            )
            account_after = executed["account"]
            positions_after = executed["positions"]
            orders = executed["orders"]
            if len(orders) != len(decisions):
                raise RuntimeError("portfolio_commit_order_count_mismatch")
            nav_record = build_nav_record(
                account_after,
                trade_date,
                positions_after,
                previous_total_assets=previous_total_assets,
                previous_twr=previous_twr,
                previous_nav_peak=previous_nav_peak,
                daily_fee=safe_float(account_after.daily_fee, 0.0),
            )
            storage.save_nav_record(nav_record)
            risk_report = calculate_portfolio_risk(user_id, account_after, positions_after, constraints)
            storage.save_risk_report(risk_report)
            decision_records = [
                _adjust_decision_record(
                    user_id=user_id,
                    plan_id=plan_id,
                    trade_date=trade_date,
                    decision_time=decision_time,
                    decision=decision,
                    order=order,
                )
                for decision, order in zip(decisions, orders)
            ]
            snapshot_paths = storage.write_daily_snapshot(
                account=account_after,
                positions=positions_after,
                orders=orders,
                risk_report=risk_report,
                decisions=decision_records,
                trade_date=trade_date,
                decision_time=_decision_time_token(decision_time),
            )
        except Exception as exc:
            if portfolio_dir.exists():
                shutil.rmtree(portfolio_dir)
            if portfolio_existed and files_backup.exists():
                shutil.copytree(files_backup, portfolio_dir)
            if not canonical_approval:
                mark_plan_revalidation_failed(
                    user_id,
                    plan_id,
                    output_dir=output_dir,
                    db_path=db_path,
                    reason=f"portfolio_commit_rolled_back:{type(exc).__name__}",
                )
            return ToolResult(
                success=False,
                message="Portfolio commit failed; file snapshots were restored and PostgreSQL write error was surfaced.",
                data={"plan_id": plan_id, "file_snapshot_rollback": True},
                errors=["portfolio_commit_rolled_back"],
                permission=ToolPermission.WRITE,
                tool_name="paper_trade_execute",
            )

    order_ids = [str(order.order_id) for order in orders]
    executed_plan = (
        {}
        if canonical_approval
        else mark_plan_executed(
            user_id,
            plan_id,
            output_dir=output_dir,
            db_path=db_path,
            order_ids=order_ids,
            execution_status="executed",
        )
    )
    write_agent_confirmation_log(
        user_id,
        plan_id=plan_id,
        confirmation_status="canonical_approved" if canonical_approval else "confirmed",
        expires_at=str(plan.get("expires_at") or ""),
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    write_agent_action_log(
        user_id,
        intent="execute_portfolio_rebalance",
        tool_name="paper_trade_execute",
        tool_input={"plan_id": plan_id},
        tool_output_summary={"order_ids": order_ids, "execution_status": "executed"},
        plan_id=plan_id,
        confirmation_status="canonical_approved" if canonical_approval else "confirmed",
        execution_status="executed",
        trade_date=trade_date,
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    return ToolResult(
        success=True,
        message=f"Executed {len(order_ids)} paper portfolio adjustment orders.",
        data={
            "plan_id": plan_id,
            "approval_id": executed_plan.get("approval_id"),
            "commit_id": f"commit_{plan_id}",
            "confirmation_status": "confirmed",
            "execution_status": "executed",
            "order_ids": order_ids,
            "position_count_after": len(positions_after),
            "risk_after": risk_report.to_dict(),
            "snapshot_paths": snapshot_paths,
        },
        permission=ToolPermission.WRITE,
        tool_name="paper_trade_execute",
    )


def _preflight_portfolio_plan(
    user_id: str,
    plan_id: str,
    plan: dict[str, Any],
    *,
    intent: str,
    output_dir: str | Path,
    db_path: str | Path | None,
) -> ToolResult | None:
    before = dict(plan.get("before") or plan.get("before_state_summary") or {})
    if before:
        current_state = query_portfolio_state(user_id, output_dir=output_dir, db_path=db_path)
        mismatches: list[str] = []
        for field in ["cash", "total_assets", "position_count"]:
            if field not in before:
                continue
            expected = safe_float(before.get(field), 0.0)
            current = safe_float(current_state.get(field), 0.0)
            if abs(expected - current) > 1e-6:
                mismatches.append(field)
        expected_positions = before.get("position_snapshot") or []
        if expected_positions:
            current_positions = []
            for item in current_state.get("positions") or []:
                row = dict(item or {})
                if safe_float(row.get("quantity"), 0.0) <= 0:
                    continue
                current_positions.append(
                    {
                        "stock_code": _stock_code(row.get("stock_code")),
                        "quantity": round(safe_float(row.get("quantity"), 0.0), 6),
                        "current_price": round(
                            safe_float(
                                row.get("current_price")
                                or row.get("last_price")
                                or row.get("close")
                                or row.get("price"),
                                0.0,
                            ),
                            6,
                        ),
                    }
                )
            current_positions.sort(key=lambda item: item["stock_code"])
            if json.dumps(expected_positions, ensure_ascii=False, sort_keys=True) != json.dumps(
                current_positions, ensure_ascii=False, sort_keys=True
            ):
                mismatches.append("position_snapshot")
        if intent == "execute_portfolio_rebalance" and plan.get("constraint_version"):
            try:
                _, _, _, live_constraints = load_user_context(
                    user_id, db_path=db_path, output_dir=output_dir
                )
                encoded = json.dumps(live_constraints, ensure_ascii=False, sort_keys=True, default=str)
                import hashlib

                live_constraint_version = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                if str(plan.get("constraint_version")) != live_constraint_version:
                    mismatches.append("constraint_version")
            except Exception:
                mismatches.append("constraint_version")
        if mismatches:
            message = "Business state changed after preview; regenerate the paper-trading proposal."
            _record_rejected_commit(
                plan,
                db_path=db_path,
                error_type="business_state_changed",
                message=message,
                result_summary={
                    "plan_id": plan_id,
                    "mismatched_fields": mismatches,
                    "business_state_version": plan.get("business_state_version"),
                },
            )
            return ToolResult(
                success=False,
                message=message,
                data={
                    "plan_id": plan_id,
                    "mismatched_fields": mismatches,
                    "business_state_version": plan.get("business_state_version"),
                },
                errors=["business_state_changed"],
                permission=ToolPermission.WRITE,
                tool_name="paper_trade_execute",
            )
    trade_date = str(plan.get("trade_date") or "")
    if trade_date and not is_trading_day(trade_date):
        message = f"{trade_date} is not an A-share trading day; no paper order was generated."
        _record_rejected_commit(
            plan,
            db_path=db_path,
            error_type="non_trading_day",
            message=message,
            result_summary={"plan_id": plan_id, "trade_date": trade_date},
        )
        return ToolResult(
            success=False,
            message=message,
            data={"plan_id": plan_id, "trade_date": trade_date},
            errors=["non_trading_day"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )
    return None


def _execute_add_stock_plan(
    user_id: str,
    plan_id: str,
    plan: dict[str, Any],
    *,
    output_dir: str | Path,
    db_path: str | Path | None,
    session_id: str,
    canonical_approval: bool = False,
) -> ToolResult:
    trade_date = str(plan.get("trade_date") or "")
    price = safe_float(plan.get("current_price"), 0.0)
    quantity = safe_float(plan.get("estimated_quantity"), 0.0)
    if not is_valid_agent_price(price) or quantity <= 0 or quantity % 100 != 0:
        message = "Invalid price or A-share lot quantity; paper order was rejected."
        _record_rejected_commit(
            plan,
            db_path=db_path,
            error_type="invalid_price_or_quantity",
            message=message,
            result_summary={"plan_id": plan_id, "price": price, "quantity": quantity},
        )
        return ToolResult(
            success=False,
            message=message,
            data={"plan_id": plan_id, "price": price, "quantity": quantity},
            errors=["invalid_price_or_quantity"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )
    target_weight = safe_float(plan.get("recommended_weight"), 0.0)
    maximum_allowed = safe_float(plan.get("maximum_allowed_weight"), 1.0)
    if target_weight > maximum_allowed + 1e-9:
        message = "Target weight exceeds the user risk cap."
        _record_rejected_commit(
            plan,
            db_path=db_path,
            error_type="risk_cap_exceeded",
            message=message,
            result_summary={
                "plan_id": plan_id,
                "target_weight": target_weight,
                "maximum_allowed_weight": maximum_allowed,
            },
        )
        return ToolResult(
            success=False,
            message=message,
            data={
                "plan_id": plan_id,
                "target_weight": target_weight,
                "maximum_allowed_weight": maximum_allowed,
            },
            errors=["risk_cap_exceeded"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )
    context = PipelineContext(
        user_id=user_id,
        trade_date=trade_date,
        decision_time=now_text(),
        top_k=1,
        output_dir=output_dir,
        db_path=db_path,
        dry_run=False,
        paper_trading_enabled=True,
        run_id=str(plan_id),
        execution_source="canonical_proposal" if canonical_approval else "agent_confirmed",
    )
    paper = run_paper_trading_pipeline(
        context,
        [dict(plan.get("recommendation_record") or {})],
        output_dir=Path(output_dir) / "portfolio" / str(user_id),
    )
    if paper.status != PipelineStatus.SUCCESS:
        _record_rejected_commit(
            plan,
            db_path=db_path,
            error_type="paper_pipeline_failed",
            message=paper.message,
            result_summary={"plan_id": plan_id, "pipeline_status": paper.status},
        )
        return ToolResult(
            success=False,
            message=paper.message,
            data={"plan_id": plan_id, "pipeline": paper.to_dict()},
            errors=list(paper.errors or ["paper_pipeline_failed"]),
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )
    order_ids = [str(order.order_id) for order in paper.orders]
    execution_status = "executed" if order_ids else "executed_no_order"
    if not canonical_approval:
        mark_plan_executed(
            user_id,
            plan_id,
            output_dir=output_dir,
            db_path=db_path,
            order_ids=order_ids,
            execution_status=execution_status,
        )
    confirmation_status = "canonical_approved" if canonical_approval else "confirmed"
    write_agent_confirmation_log(
        user_id,
        plan_id=plan_id,
        confirmation_status=confirmation_status,
        expires_at=str(plan.get("expires_at") or ""),
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    write_agent_action_log(
        user_id,
        intent="execute_add_stock",
        tool_name="paper_trade_execute",
        tool_input={"proposal_id" if canonical_approval else "plan_id": plan_id},
        tool_output_summary={"order_ids": order_ids, "execution_status": execution_status},
        plan_id=plan_id,
        confirmation_status=confirmation_status,
        execution_status=execution_status,
        trade_date=trade_date,
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    result = PaperTradeExecutionResult(
        success=bool(order_ids),
        plan_id=str(plan_id),
        confirmation_status=confirmation_status,
        execution_status=execution_status,
        order_ids=order_ids,
        position_count_after=len(paper.positions),
        message=(
            f"Executed {len(order_ids)} paper orders."
            if order_ids
            else "Plan executed but no nonzero paper order was produced."
        ),
        output_paths=dict(paper.output_paths or {}),
        warnings=list(paper.warnings or []),
        errors=[] if order_ids else ["no_order_generated"],
    )
    return ToolResult(
        success=result.success,
        message=result.message,
        data=result.to_dict(),
        warnings=result.warnings,
        errors=result.errors,
        permission=ToolPermission.WRITE,
        tool_name="paper_trade_execute",
    )


def _execute_preflighted_portfolio_plan(
    user_id: str,
    plan_id: str,
    plan: dict[str, Any],
    *,
    intent: str,
    output_dir: str | Path,
    db_path: str | Path | None,
    session_id: str,
    canonical_approval: bool,
) -> ToolResult:
    if intent == "execute_portfolio_rebalance":
        return _execute_portfolio_rebalance_plan(
            user_id,
            plan_id,
            plan,
            output_dir=output_dir,
            db_path=db_path,
            session_id=session_id,
            canonical_approval=canonical_approval,
        )
    if intent == "execute_adjust_position":
        return _execute_adjust_position_plan(
            user_id,
            plan_id,
            plan,
            output_dir=output_dir,
            db_path=db_path,
            session_id=session_id,
            canonical_approval=canonical_approval,
        )
    return _execute_add_stock_plan(
        user_id,
        plan_id,
        plan,
        output_dir=output_dir,
        db_path=db_path,
        session_id=session_id,
        canonical_approval=canonical_approval,
    )


def execute_canonical_portfolio_proposal(
    *,
    user_id: str,
    proposal_id: str,
    intent: str,
    execution_payload: dict[str, Any],
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    allowed = {"execute_add_stock", "execute_adjust_position", "execute_portfolio_rebalance"}
    if intent not in allowed:
        return ToolResult(
            success=False,
            message="Canonical portfolio Proposal has an unsupported mutation intent.",
            data={"proposal_id": proposal_id, "intent": intent},
            errors=["unsupported_proposal_intent"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )
    plan = dict(execution_payload or {})
    preflight = _preflight_portfolio_plan(
        user_id,
        proposal_id,
        plan,
        intent=intent,
        output_dir=output_dir,
        db_path=db_path,
    )
    if preflight is not None:
        return preflight
    return _execute_preflighted_portfolio_plan(
        user_id,
        proposal_id,
        plan,
        intent=intent,
        output_dir=output_dir,
        db_path=db_path,
        session_id=session_id,
        canonical_approval=True,
    )


def execute_confirmed_paper_trade_plan(
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    ok, status, plan = inspect_confirmation(
        user_id,
        plan_id,
        confirmation_token,
        output_dir=output_dir,
        db_path=db_path,
        record_failure=True,
    )
    if not ok or not plan:
        write_agent_confirmation_log(
            user_id,
            plan_id=plan_id,
            confirmation_status=status,
            session_id=session_id,
            error_message=status,
            output_dir=output_dir,
            db_path=db_path,
        )
        result = PaperTradeExecutionResult(
            success=False,
            plan_id=str(plan_id or ""),
            confirmation_status=status,
            execution_status="rejected",
            message=f"Confirmation rejected: {status}",
            errors=[status],
        )
        return ToolResult(
            success=False,
            message=result.message,
            data=result.to_dict(),
            errors=[status],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )
    intent = str(plan.get("intent") or "")
    if intent not in {"execute_add_stock", "execute_adjust_position", "execute_portfolio_rebalance"}:
        message = "Confirmation plan intent is not executable by paper_trade_execute."
        _record_rejected_commit(
            plan,
            db_path=db_path,
            error_type="unsupported_plan_intent",
            message=message,
            result_summary={"plan_id": plan_id, "intent": plan.get("intent")},
        )
        return ToolResult(
            success=False,
            message=message,
            data={"plan_id": plan_id, "intent": plan.get("intent")},
            errors=["unsupported_plan_intent"],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )
    preflight = _preflight_portfolio_plan(
        user_id,
        plan_id,
        plan,
        intent=intent,
        output_dir=output_dir,
        db_path=db_path,
    )
    if preflight is not None:
        return preflight
    ok, status, confirmed_plan = validate_confirmation(
        user_id,
        plan_id,
        confirmation_token,
        output_dir=output_dir,
        db_path=db_path,
    )
    if not ok or not confirmed_plan:
        return ToolResult(
            success=False,
            message=f"Confirmation rejected: {status}",
            data={"plan_id": plan_id, "confirmation_status": status},
            errors=[status],
            permission=ToolPermission.WRITE,
            tool_name="paper_trade_execute",
        )
    plan = confirmed_plan
    return _execute_preflighted_portfolio_plan(
        user_id,
        plan_id,
        plan,
        intent=intent,
        output_dir=output_dir,
        db_path=db_path,
        session_id=session_id,
        canonical_approval=False,
    )
