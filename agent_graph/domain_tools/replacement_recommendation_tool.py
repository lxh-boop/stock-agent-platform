from __future__ import annotations

from pathlib import Path
from typing import Any

from portfolio.target_weight_allocator import TRADE_LOT_SIZE, round_a_share_quantity

from agent_graph.domain_tools._common import first_present, normalize_stock_code, safe_float
from agent_graph.domain_tools.portfolio_state_tool import query_portfolio_state
from agent_graph.domain_tools.stock_lookup_tool import load_latest_recommendations
from agent_graph.domain_tools.tool_schemas import ReplacementCandidate, ReplacementRecommendation, ToolPermission, ToolResult


def _recommendation_by_code(
    user_id: str,
    output_dir: str | Path,
    db_path: str | Path | None,
) -> dict[str, dict[str, Any]]:
    rows = load_latest_recommendations(user_id, output_dir, db_path=db_path)
    return {
        normalize_stock_code(first_present(row, ["stock_code", "code"], "")): row
        for row in rows
        if normalize_stock_code(first_present(row, ["stock_code", "code"], ""))
    }


def recommend_replacements(
    user_id: str,
    candidate_stock_code: str,
    candidate_target_weight: float,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    limit: int = 3,
) -> ToolResult:
    state = query_portfolio_state(user_id, output_dir=output_dir, db_path=db_path)
    total_assets = safe_float((state.get("account") or {}).get("total_assets"), 0.0)
    recommendations = _recommendation_by_code(user_id, output_dir, db_path)
    candidate_code = normalize_stock_code(candidate_stock_code)
    rows: list[ReplacementCandidate] = []
    for position in state.get("positions") or []:
        code = normalize_stock_code(position.get("stock_code"))
        if not code or code == candidate_code:
            continue
        rec = recommendations.get(code, {})
        current_weight = safe_float(position.get("position_ratio"), 0.0)
        combined_adjustment = safe_float(first_present(rec, ["combined_adjustment"], 0.0), 0.0)
        position_adjustment_ratio = safe_float(first_present(rec, ["position_adjustment_ratio"], 1.0), 1.0)
        risk_warning = str(first_present(rec, ["risk_warning"], ""))
        negative_news = 1.0 if any(token in risk_warning.lower() for token in ["negative", "risk", "alert"]) else 0.0
        overweight = max(0.0, current_weight - 0.08) / 0.08
        low_contribution = max(0.0, 1.0 - position_adjustment_ratio)
        priority = (
            0.30 * max(0.0, -combined_adjustment)
            + 0.20 * max(0.0, 1.0 - position_adjustment_ratio)
            + 0.15 * negative_news
            + 0.15 * min(overweight, 1.0)
            + 0.10 * 0.0
            + 0.10 * min(low_contribution, 1.0)
        )
        reduce_weight = min(current_weight, max(0.0, float(candidate_target_weight or 0.0)))
        price = safe_float(position.get("current_price"), 0.0)
        quantity = round_a_share_quantity((reduce_weight * total_assets) / price, TRADE_LOT_SIZE) if price > 0 else 0.0
        rows.append(
            ReplacementCandidate(
                stock_code=code,
                stock_name=str(position.get("stock_name") or ""),
                current_weight=current_weight,
                recommended_weight_after=max(0.0, current_weight - reduce_weight),
                reduce_weight=reduce_weight,
                estimated_sell_quantity=quantity,
                replacement_priority_score=priority,
                replacement_reason=(
                    f"combined_adjustment={combined_adjustment:.3f}, position_adjustment_ratio={position_adjustment_ratio:.3f}, "
                    f"current_weight={current_weight:.2%}."
                ),
            )
        )
    rows = sorted(rows, key=lambda item: item.replacement_priority_score, reverse=True)[: max(0, int(limit))]
    trade_date = ""
    if recommendations:
        trade_date = str(first_present(next(iter(recommendations.values())), ["trade_date", "date"], ""))
    result = ReplacementRecommendation(
        user_id=str(user_id or "default"),
        candidate_stock_code=candidate_code,
        candidate_target_weight=float(candidate_target_weight or 0.0),
        trade_date=trade_date,
        replacement_candidates=rows,
        risk_before={"position_count": state.get("position_count"), "total_assets": total_assets},
        risk_after_estimate={
            "candidate_target_weight": float(candidate_target_weight or 0.0),
            "replacement_count": len(rows),
        },
        reason="Replacement candidates are ranked by low score, risk flags, overweight, and weak contribution.",
    )
    return ToolResult(
        success=True,
        message=result.reason,
        data=result.to_dict(),
        permission=ToolPermission.READ,
        tool_name="replacement_recommendation",
    )
