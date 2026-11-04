from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_graph.session.confirmation_manager import create_confirmation_plan
from agent_graph.domain_tools._common import first_present, is_valid_agent_price, safe_float
from agent_graph.domain_tools.audit_tool import write_agent_action_log, write_agent_confirmation_log
from agent_graph.domain_tools.portfolio_state_tool import query_portfolio_state
from agent_graph.domain_tools.position_recommendation_tool import recommend_position_weight
from agent_graph.domain_tools.replacement_recommendation_tool import recommend_replacements
from agent_graph.domain_tools.tool_schemas import PaperTradePreview, ToolPermission, ToolResult
from portfolio.target_weight_allocator import TRADE_LOT_SIZE, round_a_share_quantity


def preview_add_stock_to_paper(
    user_id: str,
    stock_code: str,
    requested_weight: float | None = None,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = 50,
    session_id: str = "",
    canonical_proposal_id: str = "",
) -> ToolResult:
    recommendation_result = recommend_position_weight(
        user_id=user_id,
        stock_code=stock_code,
        requested_weight=requested_weight,
        output_dir=output_dir,
        db_path=db_path,
        top_k=top_k,
    )
    recommendation = dict(recommendation_result.data)
    analysis = dict(recommendation.get("analysis") or {})
    state = query_portfolio_state(user_id, output_dir=output_dir, db_path=db_path)
    if not bool(state.get("safe_to_continue", state.get("consistency_status") != "rejected")):
        return ToolResult(
            success=False,
            message="Portfolio data is inconsistent, so no recommendation or paper-trade preview was created.",
            data={
                "status": "logic_error",
                "error_code": str(state.get("error_code") or "portfolio_snapshot_inconsistent"),
                "snapshot_id": str(state.get("snapshot_id") or ""),
                "safe_to_continue": False,
                "safe_to_write": False,
            },
            errors=list(state.get("consistency_errors") or ["portfolio_snapshot_inconsistent"]),
            permission=ToolPermission.PREVIEW,
            tool_name="rebalance_plan_preview",
        )
    account = dict(state.get("account") or {})
    if recommendation.get("hard_rejection"):
        return ToolResult(
            success=False,
            message="Hard risk rejected the requested paper-trading add action.",
            data={"analysis": analysis, "recommendation": recommendation},
            warnings=list(recommendation_result.warnings),
            errors=["hard_risk_rejection", "invalid_price_or_quantity"],
            permission=ToolPermission.PREVIEW,
            tool_name="rebalance_plan_preview",
        )
    if not account:
        return ToolResult(
            success=False,
            message="Paper account is missing; set paper capital before previewing an executable action.",
            data={"analysis": analysis, "recommendation": recommendation},
            warnings=["missing_paper_account"],
            errors=["missing_paper_account"],
            permission=ToolPermission.PREVIEW,
            tool_name="rebalance_plan_preview",
        )
    price = safe_float(analysis.get("current_price"), 0.0)
    quantity = safe_float(recommendation.get("estimated_quantity"), 0.0)
    if not is_valid_agent_price(price) or quantity <= 0:
        return ToolResult(
            success=False,
            message="Preview did not create a confirmation plan because price or 100-share lot quantity is invalid.",
            data={"analysis": analysis, "recommendation": recommendation},
            warnings=list(recommendation_result.warnings),
            errors=["invalid_price_or_quantity"],
            permission=ToolPermission.PREVIEW,
            tool_name="rebalance_plan_preview",
        )

    replacements = recommend_replacements(
        user_id=user_id,
        candidate_stock_code=str(analysis.get("stock_code") or ""),
        candidate_target_weight=safe_float(recommendation.get("recommended_weight"), 0.0),
        output_dir=output_dir,
        db_path=db_path,
    )
    replacement_rows = list((replacements.data or {}).get("replacement_candidates") or [])
    estimated_cost = safe_float(recommendation.get("estimated_cost"), 0.0)
    recommendation_record = {
        "stock_code": analysis.get("stock_code"),
        "stock_name": analysis.get("stock_name"),
        "trade_date": analysis.get("trade_date"),
        "current_price": price,
        "original_score": analysis.get("original_score"),
        "news_adjustment": analysis.get("news_adjustment"),
        "effective_news_adjustment": analysis.get("effective_news_adjustment"),
        "user_adjustment": analysis.get("user_adjustment"),
        "combined_adjustment": analysis.get("combined_adjustment"),
        "target_weight": recommendation.get("recommended_weight"),
        "original_target_weight": recommendation.get("recommended_weight"),
        "position_adjustment_ratio": analysis.get("position_adjustment_ratio", 0.0),
        "ai_reliability_weight": analysis.get("ai_reliability_weight", 0.0),
        "reason": recommendation.get("reason"),
        "risk_warning": recommendation.get("risk_warning") or "; ".join(analysis.get("risk_warnings") or []),
        "triggered_rules": ",".join(str(item) for item in analysis.get("triggered_rules") or []),
        "decision_id": f"agent_decision_{user_id}_{analysis.get('stock_code')}_{uuid4().hex[:8]}",
        "decision_source": "agent_control_center",
    }
    payload = {
        "operation_type": "one_time_position_operation",
        "source_type": "manual_one_time_operation",
        "expires_after_execution": True,
        "base_strategy_id": "current_enabled_strategy",
        "base_strategy_version": "",
        "stock_code": analysis.get("stock_code"),
        "stock_name": analysis.get("stock_name"),
        "trade_date": analysis.get("trade_date"),
        "current_price": price,
        "recommended_weight": recommendation.get("recommended_weight"),
        "maximum_allowed_weight": recommendation.get("maximum_allowed_weight"),
        "estimated_quantity": quantity,
        "estimated_cost": estimated_cost,
        "recommendation_record": recommendation_record,
        "analysis": analysis,
        "position_recommendation": recommendation,
        "replacement_candidates": replacement_rows,
        "before": {
            "cash": state.get("cash"),
            "total_assets": state.get("total_assets"),
            "position_count": state.get("position_count"),
        },
        "after": {
            "estimated_cash": max(0.0, safe_float(state.get("cash"), 0.0) - estimated_cost),
            "estimated_new_position_count": int(state.get("position_count") or 0) + 1,
        },
        "proposed_changes": [
            {
                "type": "one_time_add_stock",
                "stock_code": analysis.get("stock_code"),
                "target_weight": recommendation.get("recommended_weight"),
                "estimated_quantity": quantity,
            }
        ],
        "validation_results": {
            "long_term_strategy_changed": False,
            "expires_after_execution": True,
        },
        "warnings": [
            "本次操作只影响待确认的模拟盘订单，不会修改长期持仓策略。"
        ],
    }
    canonical = bool(str(canonical_proposal_id or "").strip())
    plan = (
        {
            "plan_id": str(canonical_proposal_id),
            "confirmation_token": "",
            "expires_at": "",
        }
        if canonical
        else create_confirmation_plan(
            user_id, "execute_add_stock", payload, output_dir=output_dir, db_path=db_path
        )
    )
    preview = PaperTradePreview(
        plan_id=str(plan["plan_id"]),
        confirmation_token=str(plan["confirmation_token"]),
        expires_at=str(plan["expires_at"]),
        user_id=str(user_id or "default"),
        stock_code=str(analysis.get("stock_code") or ""),
        stock_name=str(analysis.get("stock_name") or ""),
        trade_date=str(analysis.get("trade_date") or ""),
        recommended_weight=safe_float(recommendation.get("recommended_weight"), 0.0),
        estimated_quantity=quantity,
        estimated_cost=estimated_cost,
        current_price=price,
        funding_sources=[{"source": "cash", "amount": estimated_cost}],
        replacement_stocks=replacement_rows,
        before=payload["before"],
        after=payload["after"],
        risk_warning=str(recommendation.get("risk_warning") or ""),
        reason=str(recommendation.get("reason") or ""),
    )
    if not canonical:
        write_agent_confirmation_log(
            user_id,
            plan_id=preview.plan_id,
            confirmation_status="pending",
            expires_at=preview.expires_at,
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
    write_agent_action_log(
        user_id,
        intent="preview_add_stock",
        tool_name="rebalance_plan_preview",
        tool_input={"stock_code": stock_code, "requested_weight": requested_weight},
        tool_output_summary={"plan_id": preview.plan_id, "estimated_cost": preview.estimated_cost},
        plan_id=preview.plan_id,
        confirmation_status="canonical_approved" if canonical else "pending",
        execution_status="canonical_preflight" if canonical else "preview_only",
        trade_date=preview.trade_date,
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    preview_data = preview.to_dict()
    preview_data.update(
        {
            "operation_type": "one_time_position_operation",
            "source_type": "manual_one_time_operation",
            "expires_after_execution": True,
            "long_term_strategy_changed": False,
            "execution_payload": payload,
        }
    )
    return ToolResult(
        success=True,
        message="Preview created. Confirmation is required before any paper-trading write.",
        data=preview_data,
        warnings=list(recommendation_result.warnings),
        permission=ToolPermission.PREVIEW,
        tool_name="rebalance_plan_preview",
        requires_confirmation=not canonical,
        confirmation_token=preview.confirmation_token if not canonical else "",
    )


def _stock_code(value: Any) -> str:
    return str(value or "").split(".")[0].zfill(6)


def _position_price(position: dict[str, Any]) -> float:
    return safe_float(first_present(position, ["current_price", "last_price", "close", "price"], 0.0), 0.0)


def _position_weight(position: dict[str, Any], total_assets: float, price: float) -> float:
    weight = safe_float(
        first_present(position, ["position_ratio", "position_weight", "current_weight"], 0.0),
        0.0,
    )
    quantity = safe_float(position.get("quantity"), 0.0)
    if weight <= 0 and total_assets > 0 and price > 0:
        weight = quantity * price / total_assets
    return weight


def preview_adjust_position_to_weight(
    user_id: str,
    stock_code: str,
    requested_weight: float | None = None,
    position_adjustment_ratio: float | None = None,
    requested_quantity: float | None = None,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = 50,
    session_id: str = "",
    canonical_proposal_id: str = "",
) -> ToolResult:
    _ = top_k
    code = _stock_code(stock_code)
    state = query_portfolio_state(user_id, output_dir=output_dir, db_path=db_path)
    if not bool(state.get("safe_to_continue", state.get("consistency_status") != "rejected")):
        return ToolResult(
            success=False,
            message="Portfolio data is inconsistent, so no adjustment preview was created.",
            data={
                "status": "logic_error",
                "error_code": str(state.get("error_code") or "portfolio_snapshot_inconsistent"),
                "snapshot_id": str(state.get("snapshot_id") or ""),
                "safe_to_continue": False,
                "safe_to_write": False,
            },
            errors=list(state.get("consistency_errors") or ["portfolio_snapshot_inconsistent"]),
            permission=ToolPermission.PREVIEW,
            tool_name="adjust_position_preview",
        )
    account = dict(state.get("account") or {})
    positions = [dict(item or {}) for item in state.get("positions") or []]
    current = next(
        (
            item
            for item in positions
            if _stock_code(item.get("stock_code")) == code and safe_float(item.get("quantity"), 0.0) > 0
        ),
        None,
    )
    if not account or current is None:
        return ToolResult(
            success=False,
            message="The paper portfolio does not hold this stock, so no adjustment preview was created.",
            data={"stock_code": code, "account_found": bool(account)},
            errors=["missing_position"],
            permission=ToolPermission.PREVIEW,
            tool_name="adjust_position_preview",
        )

    total_assets = safe_float(first_present(account, ["total_assets", "initial_cash"], 0.0), 0.0)
    current_quantity = safe_float(current.get("quantity"), 0.0)
    current_price = _position_price(current)
    current_weight = _position_weight(current, total_assets, current_price)
    if total_assets <= 0 or not is_valid_agent_price(current_price):
        return ToolResult(
            success=False,
            message="The paper account total assets or current stock price is missing.",
            data={"stock_code": code, "total_assets": total_assets, "current_price": current_price},
            errors=["invalid_account_or_price"],
            permission=ToolPermission.PREVIEW,
            tool_name="adjust_position_preview",
        )

    requested_quantity_value = safe_float(requested_quantity, 0.0) if requested_quantity is not None else 0.0
    if requested_quantity_value > 0:
        if requested_quantity_value % TRADE_LOT_SIZE != 0:
            return ToolResult(
                success=False,
                message="Requested sell quantity is not an executable A-share lot.",
                data={"stock_code": code, "requested_quantity": requested_quantity_value, "lot_size": TRADE_LOT_SIZE},
                errors=["invalid_lot_quantity"],
                permission=ToolPermission.PREVIEW,
                tool_name="adjust_position_preview",
            )
        target_quantity = max(0.0, current_quantity - requested_quantity_value)
        target_weight = (target_quantity * current_price / total_assets) if total_assets > 0 else 0.0
        estimated_trade_quantity = current_quantity - target_quantity
        raw_target_quantity = target_quantity
        delta_quantity = target_quantity - current_quantity
    elif requested_weight is not None:
        target_weight = max(0.0, float(requested_weight))
        target_weight = min(target_weight, 1.0)
        raw_target_quantity = (target_weight * total_assets) / current_price if current_price > 0 else 0.0
        raw_delta_quantity = raw_target_quantity - current_quantity
        if target_weight <= 0:
            target_quantity = 0.0
            estimated_trade_quantity = current_quantity
        elif raw_delta_quantity < 0:
            estimated_trade_quantity = round_a_share_quantity(abs(raw_delta_quantity), TRADE_LOT_SIZE)
            target_quantity = current_quantity - estimated_trade_quantity
        else:
            estimated_trade_quantity = round_a_share_quantity(raw_delta_quantity, TRADE_LOT_SIZE)
            target_quantity = current_quantity + estimated_trade_quantity
        delta_quantity = target_quantity - current_quantity
    elif position_adjustment_ratio is not None:
        target_weight = max(0.0, current_weight * float(position_adjustment_ratio))
        target_weight = min(target_weight, 1.0)
        raw_target_quantity = (target_weight * total_assets) / current_price if current_price > 0 else 0.0
        raw_delta_quantity = raw_target_quantity - current_quantity
        if target_weight <= 0:
            target_quantity = 0.0
            estimated_trade_quantity = current_quantity
        elif raw_delta_quantity < 0:
            estimated_trade_quantity = round_a_share_quantity(abs(raw_delta_quantity), TRADE_LOT_SIZE)
            target_quantity = current_quantity - estimated_trade_quantity
        else:
            estimated_trade_quantity = round_a_share_quantity(raw_delta_quantity, TRADE_LOT_SIZE)
            target_quantity = current_quantity + estimated_trade_quantity
        delta_quantity = target_quantity - current_quantity
    else:
        return ToolResult(
            success=False,
            message="A target weight is required, for example half, exit, or target 10%.",
            data={"stock_code": code, "current_weight": current_weight},
            errors=["missing_target_weight"],
            permission=ToolPermission.PREVIEW,
            tool_name="adjust_position_preview",
        )

    if estimated_trade_quantity <= 0:
        raw_delta_quantity = raw_target_quantity - current_quantity
        raw_delta = abs(raw_delta_quantity)
        return ToolResult(
            success=False,
            message=(
                "The target change is smaller than one executable A-share lot. "
                f"Current quantity is {current_quantity:.0f}; raw target would change about {raw_delta:.1f} shares."
            ),
            data={
                "stock_code": code,
                "current_weight": current_weight,
                "target_weight": target_weight,
                "current_quantity": current_quantity,
                "target_quantity": target_quantity,
                "lot_size": TRADE_LOT_SIZE,
            },
            errors=["no_executable_lot_quantity"],
            permission=ToolPermission.PREVIEW,
            tool_name="adjust_position_preview",
        )

    trade_date = str(state.get("trade_date") or current.get("trade_date") or current.get("updated_at") or "")
    if len(trade_date) < 10:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    trade_date = trade_date[:10]
    stock_name = str(current.get("stock_name") or current.get("name") or code)
    estimated_amount = estimated_trade_quantity * current_price
    action = "reduce" if delta_quantity < 0 else "buy"
    recommendation_record = {
        "stock_code": code,
        "stock_name": stock_name,
        "trade_date": trade_date,
        "current_price": current_price,
        "target_weight": target_weight,
        "original_target_weight": target_weight,
        "position_adjustment_ratio": target_weight / current_weight if current_weight > 0 else 0.0,
        "reason": "agent requested explicit paper position adjustment",
        "risk_warning": "",
        "decision_id": f"agent_adjust_{user_id}_{code}_{uuid4().hex[:8]}",
        "decision_source": "agent_control_center",
    }
    payload = {
        "operation_type": "one_time_position_operation",
        "source_type": "manual_one_time_operation",
        "expires_after_execution": True,
        "base_strategy_id": "current_enabled_strategy",
        "base_strategy_version": "",
        "stock_code": code,
        "stock_name": stock_name,
        "trade_date": trade_date,
        "current_price": current_price,
        "current_weight": current_weight,
        "recommended_weight": target_weight,
        "target_weight": target_weight,
        "current_quantity": current_quantity,
        "target_quantity": target_quantity,
        "estimated_quantity": estimated_trade_quantity,
        "estimated_trade_quantity": estimated_trade_quantity,
        "estimated_cost": estimated_amount,
        "action": action,
        "recommendation_record": recommendation_record,
        "before": {
            "cash": state.get("cash"),
            "total_assets": state.get("total_assets"),
            "position_count": state.get("position_count"),
        },
        "after": {
            "estimated_cash": (
                safe_float(state.get("cash"), 0.0) + estimated_amount
                if action == "reduce"
                else max(0.0, safe_float(state.get("cash"), 0.0) - estimated_amount)
            ),
            "estimated_position_weight": target_weight,
        },
        "proposed_changes": [
            {
                "type": "one_time_adjust_position",
                "stock_code": code,
                "action": action,
                "current_quantity": current_quantity,
                "target_quantity": target_quantity,
                "estimated_quantity": estimated_trade_quantity,
                "target_weight": target_weight,
            }
        ],
        "validation_results": {
            "long_term_strategy_changed": False,
            "expires_after_execution": True,
        },
        "warnings": [
            "本次操作只影响待确认的模拟盘订单，不会修改长期持仓策略。"
        ],
    }
    canonical = bool(str(canonical_proposal_id or "").strip())
    plan = (
        {
            "plan_id": str(canonical_proposal_id),
            "confirmation_token": "",
            "expires_at": "",
        }
        if canonical
        else create_confirmation_plan(
            user_id, "execute_adjust_position", payload, output_dir=output_dir, db_path=db_path
        )
    )
    preview = PaperTradePreview(
        plan_id=str(plan["plan_id"]),
        confirmation_token=str(plan["confirmation_token"]),
        expires_at=str(plan["expires_at"]),
        user_id=str(user_id or "default"),
        stock_code=code,
        stock_name=stock_name,
        trade_date=trade_date,
        recommended_weight=target_weight,
        estimated_quantity=estimated_trade_quantity,
        estimated_cost=estimated_amount,
        current_price=current_price,
        funding_sources=[{"source": "position", "amount": estimated_amount, "action": action}],
        replacement_stocks=[],
        before=payload["before"],
        after=payload["after"],
        risk_warning="",
        reason="Confirmation will adjust this paper position to the target weight.",
    )
    if not canonical:
        write_agent_confirmation_log(
            user_id,
            plan_id=preview.plan_id,
            confirmation_status="pending",
            expires_at=preview.expires_at,
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
    write_agent_action_log(
        user_id,
        intent="adjust_position",
        tool_name="adjust_position_preview",
        tool_input={
            "stock_code": stock_code,
            "requested_weight": requested_weight,
            "position_adjustment_ratio": position_adjustment_ratio,
            "requested_quantity": requested_quantity,
        },
        tool_output_summary={
            "plan_id": preview.plan_id,
            "action": action,
            "estimated_quantity": preview.estimated_quantity,
        },
        plan_id=preview.plan_id,
        confirmation_status="canonical_approved" if canonical else "pending",
        execution_status="canonical_preflight" if canonical else "preview_only",
        trade_date=preview.trade_date,
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    preview_data = preview.to_dict()
    preview_data.update(
        {
            "operation_type": "one_time_position_operation",
            "source_type": "manual_one_time_operation",
            "expires_after_execution": True,
            "long_term_strategy_changed": False,
            "current_weight": current_weight,
            "target_weight": target_weight,
            "action": action,
            "current_quantity": current_quantity,
            "target_quantity": target_quantity,
            "execution_payload": payload,
        }
    )
    return ToolResult(
        success=True,
        message="Position adjustment preview created. Confirmation is required before any paper-trading write.",
        data=preview_data,
        permission=ToolPermission.PREVIEW,
        tool_name="adjust_position_preview",
        requires_confirmation=not canonical,
        confirmation_token=preview.confirmation_token if not canonical else "",
    )
