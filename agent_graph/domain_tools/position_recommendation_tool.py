from __future__ import annotations

from pathlib import Path
from portfolio.target_weight_allocator import TRADE_LOT_SIZE, round_a_share_quantity

from agent_graph.domain_tools._common import (
    action_is_hard_risk,
    cap_weight_by_risk_level,
    first_present,
    is_valid_agent_price,
    safe_float,
)
from agent_graph.domain_tools.portfolio_state_tool import query_portfolio_state
from agent_graph.domain_tools.stock_analysis_tool import analyze_stock
from agent_graph.domain_tools.tool_schemas import PositionRecommendation, ToolPermission, ToolResult
from agent_graph.domain_tools.user_profile_tool import query_user_profile


def _base_weight(position_adjustment_ratio: float, maximum_allowed: float) -> float:
    ratio = max(0.0, min(float(position_adjustment_ratio), 2.0))
    return max(0.0, min(maximum_allowed * ratio, maximum_allowed))


def recommend_position_weight(
    user_id: str,
    stock_code: str,
    requested_weight: float | None = None,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = 50,
) -> ToolResult:
    analysis_result = analyze_stock(user_id, stock_code, output_dir=output_dir, db_path=db_path, top_k=top_k)
    if not analysis_result.success:
        return analysis_result
    analysis = dict(analysis_result.data)
    profile = query_user_profile(user_id, db_path=db_path)
    risk_level = str((profile.get("risk_assessment") or {}).get("risk_level") or "C3")
    constraints = dict(profile.get("constraints") or {})
    maximum_allowed = min(
        cap_weight_by_risk_level(risk_level),
        safe_float(constraints.get("max_single_position"), cap_weight_by_risk_level(risk_level)),
    )
    state = query_portfolio_state(user_id, output_dir=output_dir, db_path=db_path)
    account = dict(state.get("account") or {})
    total_assets = safe_float(first_present(account, ["total_assets", "initial_cash"], 0.0), 0.0)
    cash = safe_float(account.get("cash"), 0.0)
    position_adjustment_ratio = safe_float(analysis.get("position_adjustment_ratio"), 1.0)
    combined_adjustment = safe_float(analysis.get("combined_adjustment"), 0.0)
    price = safe_float(analysis.get("current_price"), 0.0)
    warning = ""
    hard_rejection = action_is_hard_risk(analysis.get("final_action"), "; ".join(analysis.get("risk_warnings") or []))

    recommended_weight = _base_weight(position_adjustment_ratio, maximum_allowed)
    if hard_rejection:
        recommended_weight = 0.0
        warning = "Hard risk or excluded action rejects new paper-trading position."
    if requested_weight is not None and not hard_rejection:
        recommended_weight = min(recommended_weight or maximum_allowed, max(0.0, float(requested_weight)))
    recommended_weight = min(recommended_weight, maximum_allowed)

    amount = max(0.0, recommended_weight * total_assets)
    if not account:
        warning = "Paper account is missing; set capital before confirming any write action."
        amount = 0.0
    if not is_valid_agent_price(price):
        warning = "Missing or invalid market price; paper order cannot be created."
        amount = 0.0
    if cash > 0:
        amount = min(amount, cash)
    quantity = round_a_share_quantity(amount / price, TRADE_LOT_SIZE) if is_valid_agent_price(price) else 0.0
    cost = quantity * price
    if recommended_weight > 0 and quantity <= 0 and not warning:
        warning = "Target amount cannot buy one 100-share A-share lot."

    recommendation = PositionRecommendation(
        user_id=str(user_id or "default"),
        stock_code=str(analysis.get("stock_code") or ""),
        trade_date=str(analysis.get("trade_date") or ""),
        minimum_weight=0.0,
        recommended_weight=0.0 if warning and "cannot" in warning.lower() else recommended_weight,
        maximum_allowed_weight=maximum_allowed,
        recommended_amount=cost,
        estimated_quantity=quantity,
        lot_size=TRADE_LOT_SIZE,
        estimated_cost=cost,
        confidence="high" if position_adjustment_ratio >= 1.0 else "medium" if position_adjustment_ratio > 0 else "low",
        reason=(
            f"Risk level {risk_level} caps single-stock paper weight at {maximum_allowed:.2%}; "
            f"combined_adjustment={combined_adjustment:.3f}, position_adjustment_ratio={position_adjustment_ratio:.3f}."
        ),
        risk_warning=warning,
        hard_rejection=hard_rejection,
    )
    return ToolResult(
        success=True,
        message=recommendation.reason if not warning else f"{recommendation.reason} {warning}",
        data=recommendation.to_dict() | {"analysis": analysis},
        warnings=[warning] if warning else list(analysis_result.warnings),
        permission=ToolPermission.READ,
        tool_name="position_recommendation",
    )
