from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_graph.session.confirmation_manager import (
    create_confirmation_plan,
    inspect_confirmation,
    mark_plan_executed,
    mark_plan_revalidation_failed,
    persist_action_commit,
    validate_confirmation,
)
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from database.repositories import PredictionRepository, RecommendationRepository
from pipelines.paper_trading_pipeline import run_paper_trading_pipeline
from pipelines.schemas import PipelineContext
from portfolio.paper_trading_engine import execute_paper_rebalance
from portfolio.storage import PortfolioStorage
from strategies.runtime_resolver import StrategyRuntimeResolver


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _position_snapshot(positions: list[Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "stock_code": str(item.stock_code or ""),
            "quantity": round(float(item.quantity or 0.0), 8),
            "current_price": round(float(item.current_price or 0.0), 8),
        }
        for item in positions
        if float(item.quantity or 0.0) > 0
    ]
    return sorted(rows, key=lambda item: item["stock_code"])


class StrategyPositionService:
    """Preview and confirm applying the bound strategy to current positions."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        output_dir: str | Path = "outputs",
    ) -> None:
        self.db_path = db_path
        self.output_dir = Path(output_dir)

    @property
    def portfolio_dir(self) -> Path:
        return self.output_dir / "portfolio"

    def _storage(self, user_id: str, *, use_database: bool = True):
        return PortfolioStorage(
            self.db_path,
            output_dir=self.portfolio_dir / user_id,
            use_database=use_database,
        )

    def _recommendations(
        self,
        user_id: str,
        value: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if value:
            return [dict(item or {}) for item in value]
        recommendations = RecommendationRepository(self.db_path).list_latest(
            user_id
        )
        if recommendations:
            return recommendations
        ranking = PredictionRepository(self.db_path).list_latest_predictions()
        if ranking:
            return ranking
        raise ValueError("latest_strategy_recommendations_not_found")

    def _state(
        self,
        user_id: str,
        account_id: str,
    ) -> tuple[Any, list[Any], dict[str, Any]]:
        storage = self._storage(user_id)
        account = storage.load_account(account_id)
        if account is None:
            raise ValueError("paper_account_not_found")
        positions = storage.load_positions(user_id)
        snapshot = {
            "account_id": account.account_id,
            "cash": round(float(account.cash or 0.0), 8),
            "total_assets": round(float(account.total_assets or 0.0), 8),
            "positions": _position_snapshot(positions),
        }
        snapshot["state_hash"] = _stable_hash(snapshot)
        return account, positions, snapshot

    def preview(
        self,
        *,
        user_id: str,
        account_id: str = "",
        recommendations: list[dict[str, Any]] | None = None,
        trade_date: str = "",
        conversation_id: str = "",
        run_id: str = "",
    ) -> ToolResult:
        user_id = str(user_id or "default")
        account_id = str(account_id or f"paper_{user_id}")
        try:
            account, positions, before = self._state(
                user_id,
                account_id,
            )
            rows = self._recommendations(user_id, recommendations)
        except ValueError as exc:
            return ToolResult(
                success=False,
                message="无法生成当前持仓策略预览。",
                errors=[str(exc)],
                permission=ToolPermission.PREVIEW,
                tool_name="strategy.preview_current_position_change",
            )
        effective_date = str(
            trade_date
            or next(
                (
                    item.get("trade_date")
                    or item.get("prediction_date")
                    or item.get("date")
                    for item in rows
                    if item.get("trade_date")
                    or item.get("prediction_date")
                    or item.get("date")
                ),
                "",
            )
            or datetime.now().strftime("%Y-%m-%d")
        )[:10]
        preview_result = run_paper_trading_pipeline(
            PipelineContext(
                user_id=user_id,
                trade_date=effective_date,
                output_dir=self.output_dir,
                db_path=self.db_path,
                dry_run=True,
                paper_trading_enabled=True,
                run_id=run_id,
                execution_source="strategy_position_preview",
            ),
            rows,
        )
        if not preview_result.ok or preview_result.plan is None:
            return ToolResult(
                success=False,
                message="当前策略无法生成可执行目标组合。",
                errors=list(preview_result.errors or [preview_result.message]),
                permission=ToolPermission.PREVIEW,
                tool_name="strategy.preview_current_position_change",
            )
        storage = self._storage(user_id)
        estimated = execute_paper_rebalance(
            account,
            positions,
            preview_result.plan,
            cost_config=storage.load_trading_settings(user_id),
            persist=False,
        )
        orders = [item.to_dict() for item in estimated["orders"]]
        target_portfolio = [
            item.to_dict() for item in preview_result.plan.decisions
        ]
        after = {
            "estimated_cash": float(estimated["account"].cash or 0.0),
            "estimated_total_assets": float(
                estimated["account"].total_assets or 0.0
            ),
            "estimated_fee": sum(
                float(item.get("total_fee") or 0.0)
                for item in orders
            ),
            "estimated_position_count": len(estimated["positions"]),
        }
        payload = {
            "operation_type": (
                "confirmation_required_portfolio_operation"
            ),
            "confirmation_kind": "strategy_position_change",
            "user_id": user_id,
            "account_id": account_id,
            "trade_date": effective_date,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "strategy_id": preview_result.plan.strategy_id,
            "strategy_version": preview_result.plan.strategy_version,
            "binding_id": preview_result.plan.binding_id,
            "config_hash": preview_result.plan.config_hash,
            "resolved_config": preview_result.plan.resolved_config,
            "recommendations": rows,
            "before": before,
            "target_portfolio": target_portfolio,
            "orders_preview": orders,
            "after": after,
            "proposed_changes": orders,
            "risk_before": {
                "position_count": len(positions),
                "cash_ratio": (
                    float(account.cash or 0.0)
                    / max(float(account.total_assets or 0.0), 1.0)
                ),
            },
            "risk_after": {
                "position_count": len(estimated["positions"]),
                "cash_ratio": (
                    float(estimated["account"].cash or 0.0)
                    / max(
                        float(estimated["account"].total_assets or 0.0),
                        1.0,
                    )
                ),
            },
            "validation_results": {
                "activation_changed_positions": False,
                "preview_wrote_portfolio": False,
                "account_state_hash": before["state_hash"],
                "config_hash": preview_result.plan.config_hash,
            },
            "warnings": [
                "确认前不会修改当前持仓、订单、现金或净值。",
                "这是独立于策略注册和账户 Binding 启用的第三次确认。",
            ],
        }
        plan = create_confirmation_plan(
            user_id,
            "execute_strategy_position_change",
            payload,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        return ToolResult(
            success=True,
            message="已生成当前持仓策略调仓预览，确认前不会写入模拟盘。",
            data={
                **payload,
                "plan_id": plan["plan_id"],
                "expires_at": plan["expires_at"],
                "plan_hash": plan["plan_hash"],
                "not_committed": True,
            },
            permission=ToolPermission.PREVIEW,
            tool_name="strategy.preview_current_position_change",
            requires_confirmation=True,
            confirmation_token=str(plan["confirmation_token"]),
        )

    def commit(
        self,
        *,
        user_id: str,
        plan_id: str,
        confirmation_token: str,
        conversation_id: str = "",
    ) -> ToolResult:
        ok, status, plan = inspect_confirmation(
            user_id,
            plan_id,
            confirmation_token,
            output_dir=self.output_dir,
            db_path=self.db_path,
            record_failure=True,
        )
        if not ok or not plan:
            return self._failure(status, plan_id)
        if str(plan.get("intent") or "") != (
            "execute_strategy_position_change"
        ):
            return self._failure("unsupported_plan_intent", plan_id)
        account_id = str(plan.get("account_id") or f"paper_{user_id}")
        try:
            _, _, current = self._state(user_id, account_id)
        except ValueError as exc:
            return self._failure(str(exc), plan_id)
        if current["state_hash"] != str(
            (plan.get("before") or {}).get("state_hash") or ""
        ):
            mark_plan_revalidation_failed(
                user_id,
                plan_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
                reason="account_state_changed",
            )
            return self._failure("account_state_changed", plan_id)
        runtime = StrategyRuntimeResolver(
            db_path=self.db_path,
            output_dir=self.output_dir,
        ).resolve(
            user_id=user_id,
            account_id=account_id,
            as_of_date=str(plan.get("trade_date") or ""),
        )
        if (
            runtime.binding_id != str(plan.get("binding_id") or "")
            or runtime.config_hash != str(plan.get("config_hash") or "")
        ):
            mark_plan_revalidation_failed(
                user_id,
                plan_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
                reason="strategy_binding_or_config_changed",
            )
            return self._failure(
                "strategy_binding_or_config_changed",
                plan_id,
            )
        ok, status, confirmed = validate_confirmation(
            user_id,
            plan_id,
            confirmation_token,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        if not ok or not confirmed:
            return self._failure(status, plan_id)
        result = run_paper_trading_pipeline(
            PipelineContext(
                user_id=user_id,
                trade_date=str(plan.get("trade_date") or ""),
                output_dir=self.output_dir,
                db_path=self.db_path,
                dry_run=False,
                paper_trading_enabled=True,
                run_id=str(plan.get("run_id") or plan_id),
                execution_source="confirmed_strategy_position_change",
            ),
            [
                dict(item or {})
                for item in list(plan.get("recommendations") or [])
            ],
        )
        if not result.ok:
            persist_action_commit(
                confirmed,
                db_path=self.db_path,
                status="rejected",
                error_type="strategy_position_execution_failed",
                error_message=result.message,
            )
            return self._failure(
                "strategy_position_execution_failed",
                plan_id,
            )
        order_ids = [item.order_id for item in result.orders]
        mark_plan_executed(
            user_id,
            plan_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
            order_ids=order_ids,
            execution_status="executed",
        )
        return ToolResult(
            success=True,
            message="已按确认预览执行模拟盘策略调仓。",
            data={
                "plan_id": plan_id,
                "commit_id": f"commit_{plan_id}",
                "binding_id": result.plan.binding_id,
                "strategy_id": result.plan.strategy_id,
                "strategy_version": result.plan.strategy_version,
                "config_hash": result.plan.config_hash,
                "order_ids": order_ids,
                "positions_changed": True,
                "history_preserved": True,
                "output_paths": result.output_paths,
            },
            permission=ToolPermission.WRITE,
            tool_name="strategy.position.commit",
        )

    @staticmethod
    def _failure(error: str, plan_id: str) -> ToolResult:
        return ToolResult(
            success=False,
            message=f"当前持仓策略操作未执行：{error}",
            data={"plan_id": plan_id},
            errors=[error],
            permission=ToolPermission.WRITE,
            tool_name="strategy.position.commit",
        )
