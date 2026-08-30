from __future__ import annotations

import json
import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from pipelines.schemas import PaperTradingPipelineResult, PipelineContext, PipelineStatus, now_text
from database.repositories import (
    PredictionRepository,
    RecommendationRepository,
    RuntimeStateRepository,
    UserRepository,
)
from evaluation.reliability_updater import DEFAULT_AI_RELIABILITY_WEIGHT
from pipelines.replay_normalization import normalize_stock_code, normalize_trade_date_text
from portfolio.cash_flow import apply_cash_flows_to_account
from portfolio.paper_account import create_default_account
from portfolio.performance_metrics import build_nav_record, price_lookup_from_candidates
from portfolio.paper_trading_engine import execute_paper_rebalance
from portfolio.portfolio_risk import calculate_portfolio_risk
from portfolio.rebalance_rules import build_rebalance_plan
from portfolio.storage import PortfolioStorage
from portfolio.trading_cost_config import TradingCostConfig
from portfolio.user_profile import build_user_constraints, default_user_profile, load_user_context
from scoring.schemas import FinalRecommendationRecord, FusionOutput
from strategies.base import StrategyContext
from strategies.runtime_resolver import StrategyRuntimeResolver
from ranking_runtime.settings import ACTIVE_MODEL_NAME


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in [None, ""]:
            return default
        return float(value)
    except Exception:
        return default


def _record_to_candidate(record: FinalRecommendationRecord | FusionOutput | dict[str, Any]) -> dict[str, Any]:
    if isinstance(record, FinalRecommendationRecord):
        output = record.output
        stock_name = record.stock_name
    elif isinstance(record, FusionOutput):
        output = record
        stock_name = ""
    else:
        raw_target_weight = record.get("target_weight")
        return {
            "stock_code": record.get("stock_code") or record.get("code"),
            "stock_name": record.get("stock_name") or record.get("name") or "",
            "rank": record.get("rank")
            or record.get("pred_rank")
            or record.get("original_rank")
            or record.get("original_pred_rank"),
            "original_rank": record.get("original_rank") or record.get("original_pred_rank") or record.get("pred_rank") or record.get("rank"),
            "original_score": record.get("original_score") or record.get("original_pred_score") or record.get("pred_score") or record.get("score"),
            "score": _safe_float(record.get("original_score") or record.get("original_pred_score") or record.get("pred_score") or record.get("score"), 0.0),
            "confidence": _safe_float(record.get("confidence") or record.get("model_confidence"), 0.0),
            "risk_level": record.get("risk_level") or "medium",
            "industry": record.get("industry") or "",
            "action": "buy",
            "target_weight": _safe_float(raw_target_weight, 0.0) if raw_target_weight not in [None, ""] else "",
            "stored_target_weight": _safe_float(raw_target_weight, 0.0) if raw_target_weight not in [None, ""] else "",
            "original_target_weight": _safe_float(record.get("original_target_weight"), 0.0),
            "position_adjustment_ratio": _safe_float(record.get("position_adjustment_ratio"), 0.0),
            "ai_reliability_weight": _safe_float(record.get("ai_reliability_weight"), DEFAULT_AI_RELIABILITY_WEIGHT),
            "news_adjustment": _safe_float(record.get("news_adjustment"), 0.0),
            "user_adjustment": _safe_float(record.get("user_adjustment"), 0.0),
            "effective_news_adjustment": _safe_float(record.get("effective_news_adjustment"), 0.0),
            "combined_adjustment": _safe_float(record.get("combined_adjustment"), 0.0),
            "reason": record.get("reason") or "",
            "risk_warning": record.get("risk_warning") or "",
            "triggered_rules": "",
            "decision_id": record.get("decision_id") or record.get("source_decision_id") or "",
            "trade_date": record.get("trade_date") or record.get("prediction_date") or record.get("date") or "",
            "current_price": _safe_float(record.get("current_price") or record.get("close") or record.get("price"), 0.0),
        }
    return {
        "stock_code": output.stock_code,
        "stock_name": stock_name,
        "rank": getattr(output, "rank", None),
        "original_rank": output.original_rank,
        "original_score": output.original_score,
        "score": output.original_score,
        "confidence": getattr(output, "confidence", 0.0),
        "risk_level": "medium",
        "industry": "",
        "action": "buy",
        "target_weight": output.target_weight,
        "original_target_weight": output.original_target_weight,
        "position_adjustment_ratio": output.position_adjustment_ratio,
        "ai_reliability_weight": output.ai_reliability_weight,
        "news_adjustment": output.news_adjustment,
        "user_adjustment": output.user_adjustment,
        "effective_news_adjustment": output.effective_news_adjustment,
        "combined_adjustment": output.combined_adjustment,
        "reason": output.reason,
        "risk_warning": output.risk_warning,
        "triggered_rules": "",
        "decision_id": "",
        "trade_date": output.trade_date,
        "current_price": float(output.current_price or 0.0),
    }


def _initial_cash_from_user_profile(context: PipelineContext) -> float:
    try:
        profile = UserRepository(context.db_path).get_user_profile(context.user_id) or {}
        value = profile.get("available_capital") or profile.get("initial_cash")
        return float(value or 100000.0)
    except Exception:
        return 100000.0


def _stock_code(value: Any) -> str:
    return normalize_stock_code(value)


def _decision_time_token(value: str) -> str:
    return str(value or now_text()).replace("-", "").replace(":", "").replace(" ", "_")


def _date_text(value: str) -> str:
    try:
        return normalize_trade_date_text(value)
    except Exception:
        return ""


def _resolve_trade_date(context: PipelineContext, candidates: list[dict[str, Any]]) -> str:
    if str(context.trade_date or "").lower() != "latest":
        resolved = _date_text(context.trade_date)
        if resolved:
            return resolved
    for candidate in candidates:
        for key in ["trade_date", "date", "signal_date"]:
            resolved = _date_text(str(candidate.get(key) or ""))
            if resolved:
                return resolved
    return datetime.now().strftime("%Y-%m-%d")


def _trading_day_distance(start_date: str, end_date: str) -> int:
    try:
        start = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
        end = datetime.strptime(end_date[:10], "%Y-%m-%d").date()
    except Exception:
        return 9999
    if end <= start:
        return 0
    days = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current = current.fromordinal(current.toordinal() + 1)
    return max(0, days - 1)


def _position_holding_days(output_dir: Path, trade_date: str) -> dict[str, int]:
    orders_dir = output_dir / "history" / "orders"
    if not orders_dir.exists():
        return {}
    quantity: dict[str, float] = {}
    first_buy_date: dict[str, str] = {}
    for path in sorted(orders_dir.glob("orders_*.csv")):
        token = path.stem.replace("orders_", "")[:8]
        if len(token) != 8 or not token.isdigit():
            continue
        order_date = f"{token[:4]}-{token[4:6]}-{token[6:8]}"
        if order_date >= trade_date:
            continue
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
        except Exception:
            continue
        for row in rows:
            code = _stock_code(row.get("stock_code") or row.get("code"))
            try:
                qty = float(row.get("quantity") or 0.0)
            except Exception:
                qty = 0.0
            action = str(row.get("paper_action") or row.get("action") or "").lower()
            if qty <= 0:
                continue
            if action in {"paper_buy", "buy"}:
                if quantity.get(code, 0.0) <= 0:
                    first_buy_date[code] = order_date
                quantity[code] = quantity.get(code, 0.0) + qty
            elif action in {"paper_sell", "paper_reduce", "sell", "reduce"}:
                quantity[code] = max(0.0, quantity.get(code, 0.0) - qty)
                if quantity[code] <= 0:
                    first_buy_date.pop(code, None)
    return {code: _trading_day_distance(start, trade_date) for code, start in first_buy_date.items() if quantity.get(code, 0.0) > 0}


def _paper_action(action: str) -> str:
    return {
        "buy": "paper_buy",
        "sell": "paper_sell",
        "reduce": "paper_reduce",
        "hold": "paper_hold",
        "hold": "paper_hold",
    }.get(str(action or ""), "paper_hold")


def _paper_reason(paper_action: str, target_weight: float, current_weight: float) -> str:
    if paper_action == "paper_buy":
        return "模拟盘根据原始 Top10、数值仓位调整比例、现金和一手约束生成买入。"
    if paper_action in {"paper_sell", "paper_reduce"}:
        return "模拟盘根据目标仓位低于当前仓位、跌出缓冲区或执行约束生成卖出/减仓。"
    if paper_action == "paper_hold":
        return "模拟盘目标仓位与当前仓位差异不足以触发交易，维持当前持仓。"
    return "模拟盘未形成可执行订单，记录执行约束原因。"


def _build_paper_decisions(context: PipelineContext, plan, orders, decision_time: str) -> list[dict[str, Any]]:
    orders_by_stock = {_stock_code(order.stock_code): order for order in orders}
    records: list[dict[str, Any]] = []
    for decision in plan.decisions:
        code = _stock_code(decision.stock_code)
        order = orders_by_stock.get(code)
        paper_action = str(order.paper_action) if order else _paper_action(decision.action)
        if decision.cannot_execute_reason and not order:
            paper_action = "paper_hold"
        decision_id = decision.source_decision_id or f"paper_decision_{context.user_id}_{context.trade_date}_{code}_{_decision_time_token(decision_time)}"
        quantity = float(getattr(order, "quantity", 0.0) or 0.0)
        price = float(getattr(order, "executed_price", decision.current_price) or 0.0)
        order_amount = float(getattr(order, "order_amount", price * quantity) or 0.0)
        total_fee = float(getattr(order, "total_fee", 0.0) or 0.0)
        net_cash_change = float(getattr(order, "net_cash_change", 0.0) or 0.0)
        reason = _paper_reason(paper_action, decision.target_weight, decision.current_weight)
        if decision.reason:
            reason = f"{reason} 调仓约束说明：{decision.reason}"
        if decision.cannot_execute_reason:
            reason = f"{reason} 未成交原因：{decision.cannot_execute_reason}"
        records.append(
            {
                "decision_id": decision_id,
                "user_id": context.user_id,
                "trade_date": context.trade_date,
                "decision_time": decision_time,
                "stock_code": code,
                "stock_name": decision.stock_name,
                "paper_action": paper_action,
                "target_weight": float(decision.target_weight or 0.0),
                "current_weight": float(decision.current_weight or 0.0),
                "order_amount": order_amount,
                "order_quantity": quantity,
                "executed_price": price,
                "total_fee": total_fee,
                "net_cash_change": net_cash_change,
                "original_rank": int(getattr(decision, "original_rank", 0) or 0),
                "original_score": float(getattr(decision, "original_score", 0.0) or 0.0),
                "news_adjustment": float(getattr(decision, "news_adjustment", 0.0) or 0.0),
                "user_adjustment": float(getattr(decision, "user_adjustment", 0.0) or 0.0),
                "effective_news_adjustment": float(getattr(decision, "effective_news_adjustment", 0.0) or 0.0),
                "combined_adjustment": float(getattr(decision, "combined_adjustment", 0.0) or 0.0),
                "position_adjustment_ratio": float(getattr(decision, "position_adjustment_ratio", 1.0) or 1.0),
                "reason": reason,
                "risk_warning": decision.risk_warning,
                "triggered_rules": decision.triggered_rules,
                "source_decision_id": decision.source_decision_id,
                "job_id": context.job_id,
                "run_id": context.run_id,
                "execution_source": context.execution_source,
                "strategy_id": plan.strategy_id,
                "strategy_version": plan.strategy_version,
                "binding_id": plan.binding_id,
                "config_hash": plan.config_hash,
                "resolved_config": dict(plan.resolved_config or {}),
                "created_at": now_text(),
            }
        )
    return records


def run_paper_trading_pipeline(
    context: PipelineContext,
    final_recommendations: list[FinalRecommendationRecord | FusionOutput | dict[str, Any]],
    output_dir: str | Path | None = None,
) -> PaperTradingPipelineResult:
    if not final_recommendations:
        return PaperTradingPipelineResult(
            status=PipelineStatus.SKIPPED,
            message="No final recommendations supplied to paper trading pipeline.",
            input_count=0,
            output_count=0,
        )

    output_dir = Path(output_dir) if output_dir else context.resolved_output_dir() / "portfolio" / context.user_id
    storage = PortfolioStorage(
        context.db_path,
        output_dir=output_dir,
        use_database=True,
    )
    account = storage.load_account(f"paper_{context.user_id}") or create_default_account(
        context.user_id,
        initial_cash=_initial_cash_from_user_profile(context),
    )
    positions = storage.load_positions(context.user_id)
    candidates = [_record_to_candidate(record) for record in final_recommendations]
    trade_date = _resolve_trade_date(context, candidates)
    record_context = context.with_trade_date(trade_date)
    runtime_resolver = StrategyRuntimeResolver(
        db_path=context.db_path,
        output_dir=context.output_dir,
    )
    runtime_strategy = runtime_resolver.resolve(
        user_id=context.user_id,
        account_id=account.account_id,
        as_of_date=trade_date,
    )
    if not runtime_strategy.binding_id:
        legacy_config = {
            "entry_top_k": int(context.entry_top_k),
            "hold_buffer_rank": int(context.hold_buffer_rank),
            "max_positions": int(context.max_positions),
            "target_invested_weight": float(
                context.target_invested_weight
            ),
            "minimum_cash_ratio": float(context.minimum_cash_ratio),
            "min_rebalance_weight_delta": float(
                context.min_rebalance_weight_delta
            ),
        }
        if legacy_config != runtime_strategy.resolved_config():
            runtime_strategy = runtime_resolver.with_config(
                runtime_strategy,
                legacy_config,
            )
    resolved_config = runtime_strategy.resolved_config()
    stored_settings = storage.load_trading_settings(context.user_id)
    settings_payload = {
        key: value
        for key, value in stored_settings.to_dict().items()
        if key != "settings_id"
    }
    cost_config = TradingCostConfig(
        **{
            **settings_payload,
            "user_id": context.user_id,
            "entry_top_k": int(resolved_config["entry_top_k"]),
            "hold_buffer_rank": int(
                resolved_config["hold_buffer_rank"]
            ),
            "max_positions": int(resolved_config["max_positions"]),
            "target_invested_weight": float(
                resolved_config["target_invested_weight"]
            ),
            "minimum_cash_ratio": float(
                resolved_config["minimum_cash_ratio"]
            ),
            "min_rebalance_weight_delta": float(
                resolved_config["min_rebalance_weight_delta"]
            ),
            "strategy_mode": (
                "hierarchical_top10"
                if runtime_strategy.module_path
                == "strategies.adapters.hierarchical_top10_strategy"
                else "runtime_plugin"
            ),
            "execution_price_type": context.execution_price_type or stored_settings.execution_price_type,
        }
    )
    if not context.dry_run:
        storage.save_trading_settings(cost_config)
    try:
        _, _, _, constraints = load_user_context(
            context.user_id,
            db_path=context.db_path,
            output_dir=context.output_dir,
        )
    except Exception:
        constraints = build_user_constraints(default_user_profile(context.user_id))
    decision_time = context.decision_time or now_text()
    decision_token = _decision_time_token(decision_time)
    holding_days_by_code = _position_holding_days(output_dir, trade_date)
    for candidate in candidates:
        code = _stock_code(candidate.get("stock_code"))
        if code in holding_days_by_code:
            candidate["holding_days"] = holding_days_by_code[code]
    cash_flow_warnings: list[str] = []
    applied_flows = []
    if not context.dry_run:
        flows = storage.load_cash_flows(context.user_id)
        account, applied_flows, cash_flow_warnings = apply_cash_flows_to_account(
            account,
            flows,
            trade_date,
            db_path=context.db_path,
            output_dir=context.output_dir,
            use_database=not context.dry_run,
            persist_status=True,
        )
        if applied_flows:
            storage.save_account(account)
    for candidate in candidates:
        code = _stock_code(candidate.get("stock_code"))
        if not candidate.get("decision_id"):
            candidate["decision_id"] = f"paper_decision_{context.user_id}_{trade_date}_{code}_{decision_token}"
    plugin_result = None
    if cost_config.strategy_mode == "runtime_plugin":
        plugin_result = runtime_resolver.generate_target(
            runtime_strategy,
            StrategyContext(
                user_id=context.user_id,
                account_id=account.account_id,
                trade_date=trade_date,
                decision_time=decision_time,
                predictions=candidates,
                current_cash=float(account.cash or 0.0),
                current_positions={
                    _stock_code(item.stock_code): item.to_dict()
                    for item in positions
                },
                runtime_config={
                    **resolved_config,
                    "total_assets": float(
                        account.total_assets or account.initial_cash or 0.0
                    ),
                },
            ),
        )
        target_weights = dict(plugin_result.target_weights or {})
        for candidate in candidates:
            candidate["target_weight"] = float(
                target_weights.get(
                    _stock_code(candidate.get("stock_code")),
                    0.0,
                )
            )
    plan = build_rebalance_plan(
        user_id=context.user_id,
        trade_date=trade_date,
        candidates=candidates,
        user_constraints=constraints,
        current_positions=positions,
        account=account,
        top_k=context.top_k,
        target_invested_weight=float(
            resolved_config["target_invested_weight"]
        ),
        entry_top_k=cost_config.entry_top_k,
        hold_buffer_rank=cost_config.hold_buffer_rank,
        max_positions=cost_config.max_positions,
        minimum_cash_ratio=cost_config.minimum_cash_ratio,
        min_rebalance_weight_delta=cost_config.min_rebalance_weight_delta,
        strategy_mode=cost_config.strategy_mode,
        trading_cost_config=cost_config,
        job_id=context.job_id,
        run_id=context.run_id,
        execution_source=context.execution_source,
        strategy_id=runtime_strategy.strategy_id,
        strategy_version=runtime_strategy.strategy_version,
        binding_id=runtime_strategy.binding_id,
        config_hash=runtime_strategy.config_hash,
        resolved_config=resolved_config,
    )
    if isinstance(plan.execution_diagnostics, dict):
        plan.execution_diagnostics.update(
            {
                "original_ranking_count": len(candidates),
                "ai_adjustment_count": len(candidates),
                "stored_input_only": context.execution_source == "backfill",
                "strategy_id": runtime_strategy.strategy_id,
                "strategy_version": runtime_strategy.strategy_version,
                "binding_id": runtime_strategy.binding_id,
                "config_hash": runtime_strategy.config_hash,
                "resolved_config": resolved_config,
                "runtime_strategy_source": runtime_strategy.source,
                "plugin_result": (
                    plugin_result.to_dict() if plugin_result else None
                ),
            }
        )
    risk_report = calculate_portfolio_risk(context.user_id, account, positions, constraints)
    output_paths: dict[str, str] = {}

    if context.paper_trading_enabled and not context.dry_run:
        positions_before = [item.to_dict() for item in positions]
        cash_before = float(account.cash or 0.0)
        previous_total_assets = float(account.total_assets or account.initial_cash or 0.0)
        previous_twr = float(account.time_weighted_return or 0.0)
        executed = execute_paper_rebalance(
            account=account,
            positions=positions,
            plan=plan,
            decision_time=decision_time,
            cost_config=cost_config,
            mark_price_lookup=price_lookup_from_candidates(candidates),
            persist=True,
            storage=storage,
            trading_permissions=constraints.get(
                "trading_permissions"
            ),
        )
        if isinstance(plan.execution_diagnostics, dict):
            plan.execution_diagnostics[
                "engine_permission_blocks"
            ] = list(
                executed.get(
                    "permission_blocked_orders"
                )
                or []
            )
        account = executed["account"]
        positions = executed["positions"]
        orders = executed["orders"]
        daily_deposit = sum(float(flow.amount or 0.0) for flow in applied_flows if flow.flow_type == "deposit")
        daily_withdrawal = sum(float(flow.amount or 0.0) for flow in applied_flows if flow.flow_type == "withdrawal")
        nav_history = storage.load_nav_history(context.user_id)
        previous_nav_peak = max([float(row.get("nav_peak") or row.get("composite_nav") or row.get("nav") or 1.0) for row in nav_history] or [1.0])
        nav_record = build_nav_record(
            account,
            trade_date,
            positions,
            previous_total_assets=previous_total_assets,
            previous_twr=previous_twr,
            previous_nav_peak=previous_nav_peak,
            daily_deposit=daily_deposit,
            daily_withdrawal=daily_withdrawal,
            daily_fee=float(account.daily_fee or 0.0),
        )
        storage.save_nav_record(nav_record)
        risk_report = calculate_portfolio_risk(context.user_id, account, positions, constraints)
        storage.save_risk_report(risk_report)
        decisions = _build_paper_decisions(record_context, plan, orders, decision_time)
        decision_paths = storage.write_daily_snapshot(
            account=account,
            positions=positions,
            orders=orders,
            risk_report=risk_report,
            decisions=decisions,
            trade_date=trade_date,
            decision_time=decision_token,
            strategy_metadata={
                "strategy_id": plan.strategy_id,
                "strategy_version": plan.strategy_version,
                "binding_id": plan.binding_id,
                "config_hash": plan.config_hash,
                "resolved_config": plan.resolved_config,
            },
        )
        storage.save_strategy_execution_history(
            {
                "user_id": context.user_id,
                "account_id": account.account_id,
                "trade_date": trade_date,
                "run_id": context.run_id or decision_token,
                "strategy_id": plan.strategy_id,
                "strategy_version": plan.strategy_version,
                "binding_id": plan.binding_id,
                "config_hash": plan.config_hash,
                "resolved_config": plan.resolved_config,
                "positions_before": positions_before,
                "target_portfolio": [
                    item.to_dict() for item in plan.decisions
                ],
                "orders": [item.to_dict() for item in orders],
                "positions_after": [
                    item.to_dict() for item in positions
                ],
                "cash_before": cash_before,
                "cash_after": float(account.cash or 0.0),
            }
        )
        diagnostics_path = Path(output_dir) / "paper_execution_diagnostics_latest.json"
        RuntimeStateRepository(context.db_path).put(
            "paper_execution_diagnostics",
            plan.execution_diagnostics,
            user_id=context.user_id,
            scope_id=account.account_id,
            as_of_date=trade_date,
        )
        diagnostics_path.write_text(json.dumps(plan.execution_diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        output_paths = {
            "paper_account": str(storage.account_path),
            "paper_account_latest": str(storage.account_latest_path),
            "paper_positions": str(storage.positions_path),
            "paper_positions_latest": str(storage.positions_latest_path),
            "paper_orders": str(storage.orders_path),
            "paper_orders_latest": str(storage.orders_latest_path),
            "portfolio_risk_report": str(storage.risk_report_path),
            "portfolio_risk_report_latest": str(storage.risk_report_latest_path),
            "paper_execution_diagnostics_latest": str(diagnostics_path),
            "paper_nav_latest": str(storage.nav_latest_path),
            "composite_nav_latest": str(storage.nav_latest_path),
            "paper_trading_settings": str(storage.trading_settings_path),
            **decision_paths,
        }
        message = f"Executed {len(orders)} paper trading orders."
    else:
        orders = []
        decisions = _build_paper_decisions(record_context, plan, orders, context.decision_time or now_text())
        message = "Paper trading disabled or dry_run enabled; generated plan only."

    graph_sync: dict[str, Any] = {}
    graph_refs: list[dict[str, Any]] = []
    try:
        from agent.graph.integration import sync_portfolio_payload

        graph_sync = sync_portfolio_payload(
            user_id=context.user_id,
            portfolio_payload={
                "data": {
                    "account": account.to_dict() if hasattr(account, "to_dict") else dict(account or {}),
                    "positions": [
                        item.to_dict() if hasattr(item, "to_dict") else dict(item)
                        for item in positions
                    ],
                    "as_of_time": decision_time or trade_date,
                }
            },
            as_of_time=decision_time or trade_date,
            source_task_id=context.run_id or context.job_id or f"paper_pipeline:{context.user_id}:{trade_date}",
            source_agent_id="PAPER_TRADING_PIPELINE",
        )
        if isinstance(graph_sync.get("portfolio_ref"), dict):
            graph_refs.append(dict(graph_sync["portfolio_ref"]))
        graph_refs.extend(
            dict(item) for item in graph_sync.get("holding_refs") or [] if isinstance(item, dict)
        )
    except Exception as exc:
        # The hard-cut architecture does not silently fall back to the old entity
        # protocol. Business execution data remains persisted for audit, while the
        # pipeline is marked partial until the authoritative graph is synchronized.
        cash_flow_warnings = list(cash_flow_warnings) + [
            f"financial_graph_sync_failed:{type(exc).__name__}:{exc}"
        ]
        graph_sync = {
            "success": False,
            "status": "graph_sync_failed",
            "error": f"{type(exc).__name__}:{exc}",
        }

    result_status = PipelineStatus.SUCCESS if graph_sync.get("success") else PipelineStatus.PARTIAL
    result_message = message if graph_sync.get("success") else f"{message} Neo4j financial graph synchronization failed."
    return PaperTradingPipelineResult(
        status=result_status,
        message=result_message,
        input_count=len(final_recommendations),
        output_count=len(orders) if orders else len(plan.decisions),
        output_paths=output_paths,
        plan=plan,
        account=account,
        positions=positions,
        orders=orders,
        graph_refs=graph_refs,
        graph_sync=graph_sync,
        warnings=cash_flow_warnings,
        is_paper_trading=True,
    )


def run_paper_trading_from_latest(
    user_id: str = "default",
    top_k: int = 50,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    dry_run: bool = False,
    paper_trading_enabled: bool = True,
) -> PaperTradingPipelineResult:
    output_root = Path(output_dir)
    using_ranking_only = False
    fixed_top15_enforced = False
    top15_stock_codes: set[str] = set()
    try:
        import pandas as pd

        ranking_rows = PredictionRepository(db_path).list_latest_predictions()
        recommendation_rows = RecommendationRepository(db_path).list_latest(user_id)
        ranking = pd.DataFrame(ranking_rows)
        recommendations = pd.DataFrame(recommendation_rows)

        ranking_date = (
            str(ranking.iloc[0].get("trade_date") or "")
            if not ranking.empty
            else ""
        )
        recommendation_date = (
            str(recommendations.iloc[0].get("trade_date") or "")
            if not recommendations.empty
            else ""
        )
        if ranking_date and recommendation_date and ranking_date > recommendation_date:
            recommendations = pd.DataFrame()

        if not ranking.empty and "model_name" in ranking.columns:
            ranking_models = set(ranking["model_name"].dropna().astype(str).str.strip()) - {""}
            if ranking_models and ranking_models != {ACTIVE_MODEL_NAME}:
                raise RuntimeError(
                    f"模拟盘拒绝非主动模型排名：expected={ACTIVE_MODEL_NAME}, actual={sorted(ranking_models)}"
                )

        if not ranking.empty and "top15_up_signal" in ranking.columns:
            fixed_top15_enforced = True
            selected = ranking["top15_up_signal"].map(
                lambda value: value is True
                or str(value or "").strip().lower() in {"1", "true", "yes", "y"}
            )
            code_values = ranking.get(
                "stock_code",
                ranking.get("code", pd.Series("", index=ranking.index)),
            )
            top15_stock_codes = set(
                code_values[selected]
                .astype(str)
                .str.split(".")
                .str[0]
                .str.zfill(6)
            ) - {"", "000nan"}
            ranking = ranking[selected].copy()
            if len(top15_stock_codes) != min(15, len(selected)):
                raise RuntimeError(
                    f"固定Top15排名不完整：expected={min(15, len(selected))}, "
                    f"actual={len(top15_stock_codes)}"
                )

    except Exception as exc:
        return PaperTradingPipelineResult(
            status=PipelineStatus.FAILED,
            message=f"Failed to load latest paper trading inputs: {exc}",
            input_count=0,
            output_count=0,
            errors=[str(exc)],
        )

    if fixed_top15_enforced and not recommendations.empty:
        recommendation_codes = recommendations.get(
            "stock_code",
            recommendations.get("code", pd.Series("", index=recommendations.index)),
        )
        normalized_codes = (
            recommendation_codes.astype(str).str.split(".").str[0].str.zfill(6)
        )
        recommendations = recommendations[
            normalized_codes.isin(top15_stock_codes)
        ].copy()

    if recommendations.empty:
        if ranking.empty:
            return PaperTradingPipelineResult(
                status=PipelineStatus.SKIPPED,
                message="No latest daily result found in runtime database.",
                input_count=0,
                output_count=0,
            )
        recommendations = ranking.copy()
        using_ranking_only = True

    if top_k:
        effective_top_k = min(int(top_k), 15) if fixed_top15_enforced else int(top_k)
        recommendations = recommendations.head(effective_top_k).copy()

    if not ranking.empty and not using_ranking_only:
        try:
            if not ranking.empty:
                ranking = ranking.head(int(top_k or len(ranking))).copy()
                ranking["stock_code"] = (
                    ranking.get("stock_code", ranking.get("code", ""))
                    .astype(str)
                    .str.split(".")
                    .str[0]
                    .str.zfill(6)
                )
                recommendations["stock_code"] = (
                    recommendations.get("stock_code", recommendations.get("code", ""))
                    .astype(str)
                    .str.split(".")
                    .str[0]
                    .str.zfill(6)
                )
                ai_cols = [
                    col
                    for col in recommendations.columns
                    if col
                    not in {
                        "rank",
                        "pred_rank",
                        "original_rank",
                        "code",
                        "name",
                        "score",
                        "pred_score",
                        "target_weight",
                        "final_target_weight",
                        "final_action",
                        "action",
                        "date",
                        "trade_date",
                        "prediction_date",
                        "signal_date",
                    }
                ]
                merged = ranking.merge(
                    recommendations[ai_cols],
                    on="stock_code",
                    how="left",
                    suffixes=("", "_ai"),
                )
                if "stock_name" not in merged.columns and "name" in merged.columns:
                    merged["stock_name"] = merged["name"]
                recommendations = merged
        except Exception:
            pass

    context = PipelineContext(
        user_id=user_id,
        trade_date="latest",
        top_k=int(top_k or len(recommendations)),
        output_dir=output_root,
        db_path=db_path,
        dry_run=bool(dry_run),
        paper_trading_enabled=bool(paper_trading_enabled),
    )
    return run_paper_trading_pipeline(context, recommendations.to_dict("records"))
