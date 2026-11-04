from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from agent_graph.session.confirmation_manager import create_confirmation_plan
from agent_graph.domain_tools._common import normalize_stock_code, safe_float
from agent_graph.domain_tools.audit_tool import write_agent_action_log, write_agent_confirmation_log
from agent_graph.domain_tools.portfolio_state_tool import query_portfolio_state
from agent_graph.domain_tools.portfolio_risk_tool import query_portfolio_risk
from agent_graph.domain_tools.ranking_tool import query_ranking
from agent_graph.domain_tools.rebalance_plan_tool import (
    preview_add_stock_to_paper,
    preview_adjust_position_to_weight,
)
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from agent_graph.domain_tools.user_profile_tool import query_user_profile
from portfolio.trading_cost_config import calculate_trade_cost, default_trading_cost_config


@dataclass(frozen=True)
class ManualPositionOperationRequest:
    user_id: str
    account_id: str = ""
    trade_date: str = ""
    stock_code: str = ""
    target_weights: dict[str, float] | None = None
    target_amounts: dict[str, float] | None = None
    sell_ratios: dict[str, float] | None = None
    excluded_for_this_run: list[str] | None = None
    cash_weight: float | None = None
    target_position_count: int | None = None
    unmentioned_position_policy: str = "proportional"
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _holds_stock(state: dict[str, Any], stock_code: str) -> bool:
    code = normalize_stock_code(stock_code)
    for item in state.get("positions") or []:
        row = dict(item or {})
        if (
            normalize_stock_code(row.get("stock_code")) == code
            and safe_float(row.get("quantity"), 0.0) > 0
        ):
            return True
    return False


def _looks_like_add(query: str) -> bool:
    text = str(query or "").lower()
    return any(
        token in text
        for token in ["加入", "放入", "加到", "买入", "add", "buy"]
    )


def _looks_like_portfolio_stability_adjustment(query: str) -> bool:
    text = str(query or "").lower()
    return any(marker in text for marker in ["稳健", "更稳", "降低风险", "分散", "集中度", "stable", "robust", "conservative"])


def _position_value(row: dict[str, Any]) -> float:
    value = safe_float(row.get("market_value"), 0.0)
    if value > 0:
        return value
    return safe_float(row.get("quantity"), 0.0) * safe_float(
        row.get("current_price") or row.get("last_price") or row.get("close") or row.get("price"),
        0.0,
    )


def _auto_select_overweight_position(state: dict[str, Any]) -> dict[str, Any]:
    positions = [
        dict(item or {})
        for item in (state.get("positions") or [])
        if safe_float((item or {}).get("quantity"), 0.0) > 0
    ]
    if not positions:
        return {}
    total_assets = safe_float(state.get("total_assets") or (state.get("account") or {}).get("total_assets"), 0.0)

    def sort_key(row: dict[str, Any]) -> tuple[float, float]:
        ratio = safe_float(row.get("position_ratio") or row.get("position_weight") or row.get("current_weight"), 0.0)
        if ratio <= 0 and total_assets > 0:
            ratio = _position_value(row) / total_assets
        return ratio, _position_value(row)

    return max(positions, key=sort_key)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _position_snapshot(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in state.get("positions") or []:
        row = dict(item or {})
        quantity = safe_float(row.get("quantity"), 0.0)
        if quantity <= 0:
            continue
        rows.append(
            {
                "stock_code": normalize_stock_code(row.get("stock_code")),
                "quantity": round(quantity, 6),
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
    return sorted(rows, key=lambda item: item["stock_code"])


def _risk_snapshot_from_positions(
    positions: list[dict[str, Any]],
    *,
    total_assets: float,
    cash: float,
) -> dict[str, Any]:
    industry_exposure: dict[str, float] = {}
    weights: dict[str, float] = {}
    for row in positions:
        code = normalize_stock_code(row.get("stock_code"))
        value = safe_float(row.get("market_value"), 0.0)
        if value <= 0:
            value = safe_float(row.get("quantity"), 0.0) * safe_float(row.get("current_price"), 0.0)
        weight = value / total_assets if total_assets > 0 else 0.0
        weights[code] = weight
        industry = str(row.get("industry") or "unknown")
        industry_exposure[industry] = industry_exposure.get(industry, 0.0) + weight
    return {
        "position_count": len([row for row in positions if safe_float(row.get("quantity"), 0.0) > 0]),
        "cash_ratio": cash / total_assets if total_assets > 0 else 0.0,
        "max_single_position": max(weights.values(), default=0.0),
        "single_stock_weights": weights,
        "industry_exposure": industry_exposure,
        "max_industry_exposure": max(industry_exposure.values(), default=0.0),
    }


def _preview_stable_portfolio_rebalance(
    user_id: str,
    *,
    state: dict[str, Any],
    cash_weight: float | None,
    target_position_count: int | None,
    query: str,
    output_dir: str | Path,
    db_path: str | Path | None,
    top_k: int,
    session_id: str,
    canonical_proposal_id: str = "",
) -> ToolResult:
    positions = [
        dict(item or {})
        for item in (state.get("positions") or [])
        if safe_float((item or {}).get("quantity"), 0.0) > 0
    ]
    total_assets = safe_float(
        state.get("total_assets") or (state.get("account") or {}).get("total_assets"),
        0.0,
    )
    current_cash = safe_float(state.get("cash") or (state.get("account") or {}).get("cash"), 0.0)
    if not positions or total_assets <= 0:
        return ToolResult(
            success=False,
            message="当前模拟盘缺少可用于组合级稳健调仓的有效持仓或资产数据。",
            data={"operation_type": "one_time_position_operation", "portfolio_level": True},
            errors=["missing_portfolio_state"],
            permission=ToolPermission.PREVIEW,
            tool_name="manual_position_operation_tool",
        )

    profile_context = query_user_profile(user_id, db_path=db_path, output_dir=output_dir)
    constraints = dict(profile_context.get("constraints") or {})
    max_single = max(0.01, min(1.0, safe_float(constraints.get("max_single_position"), 0.08)))
    max_industry = max(max_single, min(1.0, safe_float(constraints.get("max_industry_position"), 0.30)))
    requested_cash = None if cash_weight is None else max(0.0, min(1.0, float(cash_weight)))
    minimum_cash = max(current_cash / total_assets, requested_cash or 0.0)

    ranking = query_ranking(
        top_k=max(10, int(top_k or 50)), output_dir=output_dir, db_path=db_path
    )
    ranking_rows = [dict(item or {}) for item in (ranking.get("records") or [])]
    ranking_by_code = {
        normalize_stock_code(row.get("code") or row.get("stock_code")): row
        for row in ranking_rows
        if normalize_stock_code(row.get("code") or row.get("stock_code"))
    }
    current_risk = query_portfolio_risk(user_id, output_dir=output_dir, db_path=db_path)
    cost_config = default_trading_cost_config(user_id)

    before_positions: list[dict[str, Any]] = []
    target_positions: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    estimated_cash = current_cash
    invalid_prices: list[str] = []
    lot_checks: list[dict[str, Any]] = []

    for row in positions:
        code = normalize_stock_code(row.get("stock_code"))
        quantity = safe_float(row.get("quantity"), 0.0)
        price = safe_float(
            row.get("current_price") or row.get("last_price") or row.get("close") or row.get("price"),
            0.0,
        )
        if not code or quantity <= 0 or price <= 0:
            invalid_prices.append(code or "unknown")
            continue
        market_value = quantity * price
        current_weight = market_value / total_assets
        raw_target_quantity = min(quantity, max_single * total_assets / price)
        target_quantity = min(quantity, math.floor(raw_target_quantity / 100.0) * 100.0)
        if current_weight <= max_single + 1e-9:
            target_quantity = quantity
        sell_quantity = max(0.0, quantity - target_quantity)
        rank_row = ranking_by_code.get(code) or {}
        rank_value = int(safe_float(rank_row.get("rank"), 0.0)) or None
        reasons = []
        if sell_quantity > 0:
            reasons.append(f"当前仓位 {current_weight:.2%} 超过用户单股上限 {max_single:.2%}")
        else:
            reasons.append("当前仓位未超过用户单股约束，保持不变")
        if rank_value:
            reasons.append(f"最新模型排名第 {rank_value} 名，仅作为持仓保留/减仓解释依据")
        else:
            reasons.append("最新模型排名中未找到该持仓，不据此新增证券")
        if rank_row.get("risk_level"):
            reasons.append(f"模型风险等级 {rank_row.get('risk_level')}")

        before_positions.append(
            {
                "stock_code": code,
                "stock_name": row.get("stock_name") or row.get("name") or code,
                "industry": row.get("industry") or "unknown",
                "quantity": quantity,
                "current_price": price,
                "market_value": market_value,
                "current_weight": current_weight,
            }
        )
        target_value = target_quantity * price
        target_weight = target_value / total_assets
        target_positions.append(
            {
                "stock_code": code,
                "stock_name": row.get("stock_name") or row.get("name") or code,
                "industry": row.get("industry") or "unknown",
                "current_price": price,
                "current_quantity": quantity,
                "target_quantity": target_quantity,
                "current_weight": current_weight,
                "target_weight": target_weight,
                "direction": "reduce" if sell_quantity > 0 else "hold",
                "reason": "；".join(reasons),
                "ranking": {
                    "rank": rank_value,
                    "score": rank_row.get("score"),
                    "risk_score": rank_row.get("risk_score"),
                    "risk_level": rank_row.get("risk_level"),
                    "model_name": rank_row.get("model_name"),
                    "news_adjustment": rank_row.get("news_adjustment"),
                    "user_adjustment": rank_row.get("user_adjustment"),
                },
            }
        )
        lot_checks.append(
            {
                "stock_code": code,
                "current_quantity": quantity,
                "target_quantity": target_quantity,
                "sell_quantity": sell_quantity,
                "lot_size": 100,
                "executable": sell_quantity == 0 or (sell_quantity > 0 and sell_quantity % 100 == 0),
            }
        )
        if sell_quantity > 0:
            costs = calculate_trade_cost("sell", sell_quantity * price, cost_config)
            estimated_cash += safe_float(costs.get("net_cash_change"), 0.0)
            changes.append(
                {
                    "type": "portfolio_reduce_position",
                    "stock_code": code,
                    "stock_name": row.get("stock_name") or row.get("name") or code,
                    "action": "reduce",
                    "current_quantity": quantity,
                    "target_quantity": target_quantity,
                    "estimated_quantity": sell_quantity,
                    "current_price": price,
                    "current_weight": current_weight,
                    "target_weight": target_weight,
                    "estimated_fee": safe_float(costs.get("total_fee"), 0.0),
                    "reason": "；".join(reasons),
                }
            )

    if invalid_prices:
        return ToolResult(
            success=False,
            message="部分持仓缺少有效价格，无法生成可执行的一手组合预案。",
            data={"invalid_price_stocks": invalid_prices, "portfolio_level": True},
            errors=["invalid_position_price"],
            permission=ToolPermission.PREVIEW,
            tool_name="manual_position_operation_tool",
        )
    if not changes:
        return ToolResult(
            success=False,
            message="当前持仓已满足用户单股仓位约束，没有生成无意义的写操作预案。",
            data={
                "portfolio_level": True,
                "current_positions": before_positions,
                "target_positions": target_positions,
                "constraints": constraints,
            },
            warnings=["可继续使用只读持仓风险分析，不需要提交模拟盘订单。"],
            errors=["portfolio_already_within_constraints"],
            permission=ToolPermission.PREVIEW,
            tool_name="manual_position_operation_tool",
        )

    after_positions = [
        {
            **row,
            "quantity": row["target_quantity"],
            "market_value": row["target_quantity"] * row["current_price"],
        }
        for row in target_positions
        if safe_float(row.get("target_quantity"), 0.0) > 0
    ]
    before_risk = _risk_snapshot_from_positions(before_positions, total_assets=total_assets, cash=current_cash)
    after_risk = _risk_snapshot_from_positions(after_positions, total_assets=total_assets, cash=estimated_cash)
    target_stock_weight = sum(safe_float(row.get("target_weight"), 0.0) for row in target_positions)
    target_cash_weight = max(0.0, 1.0 - target_stock_weight)
    state_snapshot = _position_snapshot(state)
    constraint_version = _stable_hash(constraints)
    ranking_version = _stable_hash(
        {
            "as_of_date": ranking.get("as_of_date"),
            "model_names": sorted({str(row.get("model_name") or "") for row in ranking_rows}),
            "returned_count": len(ranking_rows),
        }
    )
    trade_date = str(ranking.get("as_of_date") or state.get("trade_date") or state.get("as_of_date") or "")[:10]
    if len(trade_date) != 10:
        trade_date = datetime.now().strftime("%Y-%m-%d")
    payload = {
        "operation_type": "one_time_position_operation",
        "source_type": "deterministic_stable_portfolio_rebalance",
        "portfolio_level": True,
        "trade_date": trade_date,
        "query": query,
        "expires_after_execution": True,
        "base_strategy_id": "current_enabled_strategy",
        "base_strategy_version": "",
        "constraint_version": constraint_version,
        "ranking_version": ranking_version,
        "before": {
            "cash": current_cash,
            "total_assets": total_assets,
            "position_count": len(before_positions),
            "position_snapshot": state_snapshot,
            "position_snapshot_hash": _stable_hash(state_snapshot),
            "constraint_version": constraint_version,
        },
        "after": {
            "estimated_cash": estimated_cash,
            "estimated_cash_ratio": target_cash_weight,
            "estimated_position_count": len(after_positions),
            "total_target_weight": target_stock_weight,
        },
        "current_positions": before_positions,
        "target_positions": target_positions,
        "proposed_changes": changes,
        "one_lot_checks": lot_checks,
        "unallocated_cash": estimated_cash,
        "profile_context": {
            "profile": profile_context.get("profile") or {},
            "risk_assessment": profile_context.get("risk_assessment") or {},
            "investment_goal": profile_context.get("investment_goal") or {},
            "constraints": constraints,
        },
        "ranking_context": {
            "as_of_date": ranking.get("as_of_date"),
            "model_names": sorted({str(row.get("model_name") or "") for row in ranking_rows if row.get("model_name")}),
            "held_stock_matches": sum(1 for row in target_positions if (row.get("ranking") or {}).get("rank")),
            "candidate_count": len(ranking_rows),
            "candidate_policy": "current_holdings_only_for_stability_write; ranking_used_for_explanation",
            "news_adjustment_available": any(row.get("news_adjustment") is not None for row in ranking_rows),
            "user_adjustment_available": any(row.get("user_adjustment") is not None for row in ranking_rows),
        },
        "risk_before": before_risk,
        "risk_after": after_risk,
        "stored_risk_report": current_risk.get("risk_report") or {},
        "validation_results": {
            "long_term_strategy_changed": False,
            "expires_after_execution": True,
            "paper_trading_only": True,
            "all_orders_round_lot": all(bool(row.get("executable")) for row in lot_checks),
            "single_position_limit": max_single,
            "industry_position_limit": max_industry,
            "target_max_single_position": after_risk.get("max_single_position"),
            "target_max_industry_exposure": after_risk.get("max_industry_exposure"),
            "weights_plus_cash": target_stock_weight + target_cash_weight,
            "requested_minimum_cash_weight": minimum_cash,
            "target_position_count_requested": target_position_count,
        },
        "warnings": [
            "这是模拟盘的一次性组合调仓待确认预案，确认前不会写入持仓。",
            "确定性预案只对现有超限持仓减仓，不新增股票，也不修改长期策略。",
        ],
    }
    canonical = bool(str(canonical_proposal_id or "").strip())
    plan = (
        {
            "plan_id": str(canonical_proposal_id),
            "confirmation_token": "",
            "expires_at": "",
            "plan_hash": "",
            "business_state_version": "",
        }
        if canonical
        else create_confirmation_plan(
            user_id,
            "execute_portfolio_rebalance",
            payload,
            output_dir=output_dir,
            db_path=db_path,
        )
    )
    data = {
        **payload,
        "plan_id": plan["plan_id"],
        "confirmation_token": plan["confirmation_token"],
        "expires_at": plan["expires_at"],
        "plan_hash": plan["plan_hash"],
        "business_state_version": plan["business_state_version"],
        "not_committed": True,
        "long_term_strategy_changed": False,
    }
    if not canonical:
        write_agent_confirmation_log(
            user_id,
            plan_id=str(plan["plan_id"]),
            confirmation_status="pending",
            expires_at=str(plan["expires_at"]),
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
    write_agent_action_log(
        user_id,
        intent="one_time_position_operation",
        tool_name="manual_position_operation_tool",
        tool_input={"query": query, "portfolio_level": True},
        tool_output_summary={"plan_id": plan["plan_id"], "change_count": len(changes)},
        plan_id=str(plan["plan_id"]),
        confirmation_status="canonical_approved" if canonical else "pending",
        execution_status="canonical_preflight" if canonical else "preview_only",
        trade_date=trade_date,
        session_id=session_id,
        output_dir=output_dir,
        db_path=db_path,
    )
    return ToolResult(
        success=True,
        message=f"已生成组合级稳健调仓预案，共 {len(changes)} 只持仓需要减仓；确认前不会修改模拟盘。",
        data=data,
        warnings=list(payload["warnings"]),
        permission=ToolPermission.PREVIEW,
        tool_name="manual_position_operation_tool",
        requires_confirmation=not canonical,
        confirmation_token=str(plan["confirmation_token"]) if not canonical else "",
    )


def preview_manual_position_operation(
    user_id: str,
    stock_code: str | None = None,
    requested_weight: float | None = None,
    position_adjustment_ratio: float | None = None,
    requested_quantity: float | None = None,
    cash_weight: float | None = None,
    target_position_count: int | None = None,
    query: str = "",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = 50,
    session_id: str = "",
    canonical_proposal_id: str = "",
) -> ToolResult:
    code = normalize_stock_code(stock_code)
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
            tool_name="manual_position_operation_tool",
        )
    if not code:
        if _looks_like_portfolio_stability_adjustment(query):
            return _preview_stable_portfolio_rebalance(
                user_id,
                state=state,
                cash_weight=cash_weight,
                target_position_count=target_position_count,
                query=query,
                output_dir=output_dir,
                db_path=db_path,
                top_k=top_k,
                session_id=session_id,
                canonical_proposal_id=canonical_proposal_id,
            )
        return ToolResult(
            success=False,
            message=(
                "一次性仓位操作需要明确股票代码；当前尚不支持把整组 "
                "TopK 目标持仓直接作为可执行订单。"
            ),
            data={
                "operation_type": "one_time_position_operation",
                "source_type": "manual_one_time_operation",
                "cash_weight": cash_weight,
                "target_position_count": target_position_count,
                "query": query,
            },
            warnings=[
                "本次操作不会修改长期策略；缺少股票代码时不会生成订单预览。"
            ],
            errors=["missing_stock_code_for_manual_operation"],
            permission=ToolPermission.PREVIEW,
            tool_name="manual_position_operation_tool",
        )

    held = _holds_stock(state, code)

    if held or requested_quantity is not None or position_adjustment_ratio is not None or not _looks_like_add(query):
        result = preview_adjust_position_to_weight(
            user_id,
            code,
            requested_weight=requested_weight,
            position_adjustment_ratio=position_adjustment_ratio,
            requested_quantity=requested_quantity,
            output_dir=output_dir,
            db_path=db_path,
            top_k=top_k,
            session_id=session_id,
            canonical_proposal_id=canonical_proposal_id,
        )
    else:
        result = preview_add_stock_to_paper(
            user_id,
            code,
            requested_weight=requested_weight,
            output_dir=output_dir,
            db_path=db_path,
            top_k=top_k,
            session_id=session_id,
            canonical_proposal_id=canonical_proposal_id,
        )

    data = dict(result.data or {})
    data.update(
        {
            "operation_type": "one_time_position_operation",
            "source_type": "manual_one_time_operation",
            "expires_after_execution": True,
            "base_strategy_id": data.get("base_strategy_id") or "current_enabled_strategy",
            "base_strategy_version": data.get("base_strategy_version") or "",
            "long_term_strategy_changed": False,
            "manual_request": ManualPositionOperationRequest(
                user_id=str(user_id or "default"),
                account_id=str((state.get("account") or {}).get("account_id") or ""),
                trade_date=str(data.get("trade_date") or ""),
                stock_code=code,
                target_weights={code: float(requested_weight)}
                if requested_weight is not None
                else None,
                sell_ratios={code: float(position_adjustment_ratio)}
                if position_adjustment_ratio is not None
                else None,
                cash_weight=cash_weight,
                target_position_count=target_position_count,
                reason=query or "manual one-time paper position operation",
            ).to_dict(),
        }
    )
    warnings = list(result.warnings or [])
    warnings.append("本次操作只影响待确认的模拟盘订单，不会修改长期持仓策略。")
    return ToolResult(
        success=bool(result.success),
        message=result.message,
        data=data,
        warnings=warnings,
        errors=list(result.errors or []),
        permission=ToolPermission.PREVIEW,
        tool_name="manual_position_operation_tool",
        requires_confirmation=bool(data.get("plan_id")) and not bool(canonical_proposal_id),
        confirmation_token=data.get("confirmation_token") if not canonical_proposal_id else "",
    )
