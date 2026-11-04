from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    return {}


def _wrap_result(
    result: ToolResult,
    *,
    tool_name: str,
    permission: str | None = None,
    data_patch: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> ToolResult:
    data = dict(result.data or {})
    data.update(dict(data_patch or {}))
    merged_warnings = list(result.warnings or [])
    merged_warnings.extend(item for item in (warnings or []) if item)
    return ToolResult(
        success=bool(result.success),
        message=str(result.message or ""),
        data=data,
        warnings=merged_warnings,
        errors=list(result.errors or []),
        permission=permission or result.permission,
        tool_name=tool_name,
        disclaimer=result.disclaimer,
        status=result.status,
        requires_confirmation=bool(result.requires_confirmation),
        confirmation_token=result.confirmation_token,
    )


class PortfolioProposalService:
    """Domain service for portfolio advice, proposal previews and paper-trade commits.

    The existing portfolio algorithms remain in the legacy tool modules. This service
    centralizes the Agent-facing contract without changing one-lot, cash allocation,
    revalidation, or idempotency rules.
    """

    def recommend_position(
        self,
        user_id: str,
        stock_code: str,
        requested_weight: float | None = None,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        top_k: int = 50,
    ) -> ToolResult:
        from agent_graph.domain_tools.position_recommendation_tool import recommend_position_weight

        result = recommend_position_weight(
            user_id,
            stock_code,
            requested_weight=requested_weight,
            output_dir=output_dir,
            db_path=db_path,
            top_k=top_k,
        )
        data = dict(result.data or {})
        analysis = _as_dict(data.get("analysis"))
        stock = str(data.get("stock_code") or analysis.get("stock_code") or stock_code or "")
        recommended_weight = data.get("recommended_weight")
        patch = {
            "candidate_stocks": [stock] if stock else [],
            "target_weights": {stock: recommended_weight} if stock else {},
            "cash_ratio": None,
            "current_vs_target": {
                "stock_code": stock,
                "target_weight": recommended_weight,
                "estimated_quantity": data.get("estimated_quantity"),
            },
            "risk_notes": [text for text in [data.get("risk_warning")] if text],
            "assumptions": [
                "Uses latest local ranking, user risk profile and current paper account state.",
                "No paper-trading write is performed by this recommendation.",
            ],
            "not_executed": True,
        }
        return _wrap_result(
            result,
            tool_name="portfolio.recommend_position",
            permission=ToolPermission.READ,
            data_patch=patch,
        )

    def recommend_replacement(
        self,
        user_id: str,
        stock_code: str,
        requested_weight: float | None = None,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        limit: int = 3,
    ) -> ToolResult:
        from agent_graph.domain_tools.replacement_recommendation_tool import recommend_replacements

        result = recommend_replacements(
            user_id,
            stock_code,
            float(requested_weight if requested_weight is not None else 0.05),
            output_dir=output_dir,
            db_path=db_path,
            limit=limit,
        )
        data = dict(result.data or {})
        patch = {
            "source_stock": data.get("candidate_stock_code") or stock_code,
            "score_comparison": data.get("replacement_candidates") or [],
            "risk_comparison": {
                "before": data.get("risk_before") or {},
                "after_estimate": data.get("risk_after_estimate") or {},
            },
            "not_executed": True,
        }
        return _wrap_result(
            result,
            tool_name="portfolio.recommend_replacement",
            permission=ToolPermission.READ,
            data_patch=patch,
        )

    def preview_manual_position_change(
        self,
        user_id: str,
        *,
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
    ) -> ToolResult:
        from agent_graph.domain_tools.manual_position_operation_tool import preview_manual_position_operation

        result = preview_manual_position_operation(
            user_id,
            stock_code=stock_code,
            requested_weight=requested_weight,
            position_adjustment_ratio=position_adjustment_ratio,
            requested_quantity=requested_quantity,
            cash_weight=cash_weight,
            target_position_count=target_position_count,
            query=query,
            output_dir=output_dir,
            db_path=db_path,
            top_k=top_k,
            session_id=session_id,
        )
        return self._proposal_contract(result, tool_name="portfolio.preview_manual_change")

    def preview_rebalance(
        self,
        user_id: str,
        stock_code: str,
        requested_weight: float | None = None,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        top_k: int = 50,
        session_id: str = "",
    ) -> ToolResult:
        from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper

        result = preview_add_stock_to_paper(
            user_id,
            stock_code,
            requested_weight=requested_weight,
            output_dir=output_dir,
            db_path=db_path,
            top_k=top_k,
            session_id=session_id,
        )
        return self._proposal_contract(result, tool_name="portfolio.preview_rebalance")

    def preview_adjust_position(
        self,
        user_id: str,
        stock_code: str,
        requested_weight: float | None = None,
        *,
        position_adjustment_ratio: float | None = None,
        requested_quantity: float | None = None,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        top_k: int = 50,
        session_id: str = "",
    ) -> ToolResult:
        from agent_graph.domain_tools.rebalance_plan_tool import preview_adjust_position_to_weight

        result = preview_adjust_position_to_weight(
            user_id,
            stock_code,
            requested_weight=requested_weight,
            position_adjustment_ratio=position_adjustment_ratio,
            requested_quantity=requested_quantity,
            output_dir=output_dir,
            db_path=db_path,
            top_k=top_k,
            session_id=session_id,
        )
        return self._proposal_contract(result, tool_name="portfolio.preview_adjust_position")

    def preview_paper_trade(
        self,
        user_id: str,
        stock_code: str,
        requested_weight: float | None = None,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        top_k: int = 50,
        session_id: str = "",
    ) -> ToolResult:
        from agent_graph.domain_tools.paper_trade_preview_tool import preview_paper_trade

        result = preview_paper_trade(
            user_id,
            stock_code,
            requested_weight=requested_weight,
            output_dir=output_dir,
            db_path=db_path,
            top_k=top_k,
            session_id=session_id,
        )
        return self._proposal_contract(result, tool_name="portfolio.preview_paper_trade")

    def commit_paper_trade(
        self,
        user_id: str,
        plan_id: str,
        confirmation_token: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        session_id: str = "",
    ) -> ToolResult:
        from agent_graph.domain_tools.paper_trade_execute_tool import execute_confirmed_paper_trade_plan

        result = execute_confirmed_paper_trade_plan(
            user_id,
            plan_id,
            confirmation_token,
            output_dir=output_dir,
            db_path=db_path,
            session_id=session_id,
        )
        data = dict(result.data or {})
        patch = {
            "revalidation_result": {
                "status": "passed" if result.success else "failed",
                "errors": list(result.errors or []),
            },
            "commit_result": data,
            "audit_record": {
                "plan_id": plan_id,
                "tool_name": "portfolio.commit_paper_trade",
                "legacy_commit_engine": "paper_trade_execute_tool.execute_confirmed_paper_trade_plan",
            },
        }
        return _wrap_result(
            result,
            tool_name="portfolio.commit_paper_trade",
            permission=ToolPermission.WRITE,
            data_patch=patch,
        )

    def _proposal_contract(self, result: ToolResult, *, tool_name: str) -> ToolResult:
        data = dict(result.data or {})
        before = _as_dict(data.get("before"))
        after = _as_dict(data.get("after"))
        patch = {
            "order_preview": data.get("proposed_changes") or [
                {
                    "stock_code": data.get("stock_code"),
                    "quantity": data.get("estimated_quantity") or data.get("estimated_trade_quantity"),
                    "target_weight": data.get("target_weight") or data.get("recommended_weight"),
                    "action": data.get("action"),
                }
            ],
            "cash_impact": {
                "before": before.get("cash"),
                "after": after.get("estimated_cash"),
            },
            "risk_impact": data.get("validation_results") or {},
            "confirmation_plan": {
                "plan_id": data.get("plan_id"),
                "confirmation_token_present": bool(data.get("confirmation_token") or result.confirmation_token),
                "expires_at": data.get("expires_at"),
            },
            "current_positions": data.get("current_positions") or [],
            "target_positions": data.get("target_positions") or [],
            "orders_preview": data.get("proposed_changes") or [],
            "cash_before_after": {"before": before.get("cash"), "after": after.get("estimated_cash")},
            "one_lot_check": {
                "estimated_quantity": data.get("estimated_quantity") or data.get("estimated_trade_quantity"),
                "lot_size": 100,
            },
            "unallocated_cash": data.get("unallocated_cash"),
            "risk_before_after": {"before": before, "after": after},
            "not_committed": True,
        }
        return _wrap_result(
            result,
            tool_name=tool_name,
            permission=ToolPermission.PREVIEW,
            data_patch=patch,
        )


portfolio_proposal_service = PortfolioProposalService()
