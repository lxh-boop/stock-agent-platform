"""Deterministic canonical-Portfolio mutation adapter (never an LLM Worker)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.session.proposals import ProposalArtifact


def _plain_result(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_legacy_dict"):
        try:
            return dict(value.to_legacy_dict())
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return dict(value.to_dict())
        except Exception:
            pass
    return dict(value or {}) if isinstance(value, dict) else {"success": False, "message": str(value)}


def _execution_parameters(proposal: ProposalArtifact) -> dict[str, Any]:
    payload = dict(proposal.payload or {})
    params = payload.get("execution_parameters") if isinstance(payload.get("execution_parameters"), dict) else {}
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
    merged = {**target, **changes, **params}
    accepted = {
        "stock_code",
        "requested_weight",
        "position_adjustment_ratio",
        "requested_quantity",
        "cash_weight",
        "target_position_count",
        "top_k",
        "query",
    }
    return {key: value for key, value in merged.items() if key in accepted and value is not None}


def _build_execution_preview(
    proposal: ProposalArtifact,
    *,
    user_id: str,
    session_id: str,
    output_dir: str | Path,
    db_path: str | Path | None,
) -> tuple[dict[str, Any], str]:
    from agent_graph.domain_tools.manual_position_operation_tool import preview_manual_position_operation
    from agent_graph.domain_tools.rebalance_plan_tool import (
        preview_add_stock_to_paper,
        preview_adjust_position_to_weight,
    )

    args = _execution_parameters(proposal)
    operation = str((proposal.payload or {}).get("operation_type") or "").strip().lower()
    common = {
        "user_id": str(user_id or "default"),
        "output_dir": output_dir,
        "db_path": db_path,
        "top_k": int(args.get("top_k") or 50),
        "session_id": str(session_id or ""),
        "canonical_proposal_id": proposal.proposal_id,
    }
    stock_code = str(args.get("stock_code") or "")
    if operation in {"add", "add_position"}:
        preview = preview_add_stock_to_paper(
            stock_code=stock_code,
            requested_weight=args.get("requested_weight"),
            **common,
        )
        intent = "execute_add_stock"
    elif operation in {"reduce", "reduce_position", "sell", "adjust", "adjust_position", "target_weight"}:
        preview = preview_adjust_position_to_weight(
            stock_code=stock_code,
            requested_weight=args.get("requested_weight"),
            position_adjustment_ratio=args.get("position_adjustment_ratio"),
            requested_quantity=args.get("requested_quantity"),
            **common,
        )
        intent = "execute_adjust_position"
    else:
        preview = preview_manual_position_operation(
            stock_code=stock_code or None,
            requested_weight=args.get("requested_weight"),
            position_adjustment_ratio=args.get("position_adjustment_ratio"),
            requested_quantity=args.get("requested_quantity"),
            cash_weight=args.get("cash_weight"),
            target_position_count=args.get("target_position_count"),
            query=str(args.get("query") or (proposal.payload or {}).get("rationale") or ""),
            **common,
        )
        preview_data = dict(preview.data or {})
        intent = (
            "execute_portfolio_rebalance"
            if bool(preview_data.get("portfolio_level"))
            else "execute_adjust_position"
            if str(preview_data.get("action") or "")
            else "execute_add_stock"
        )
    plain = _plain_result(preview)
    data = dict(plain.get("data") or {})
    execution_payload = data.get("execution_payload")
    if not isinstance(execution_payload, dict):
        execution_payload = data
    return {**plain, "execution_payload": dict(execution_payload or {})}, intent


def _canonical_commit_handler(args: dict[str, Any], context: dict[str, Any]) -> Any:
    from agent_graph.domain_tools.paper_trade_execute_tool import execute_canonical_portfolio_proposal

    return execute_canonical_portfolio_proposal(
        user_id=str(args.get("user_id") or "default"),
        proposal_id=str(args.get("proposal_id") or ""),
        intent=str(args.get("intent") or ""),
        execution_payload=dict(args.get("execution_payload") or {}),
        output_dir=context.get("output_dir") or "outputs",
        db_path=context.get("db_path"),
        session_id=str(context.get("session_id") or ""),
    )


class PortfolioMutationAdapter:
    can_mutate = True
    execution_stage = "mutation"
    execution_mode = "canonical_proposal"

    def execute(
        self,
        *,
        proposal: ProposalArtifact,
        user_id: str,
        session_id: str,
        run_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        if proposal.proposal_type != "portfolio_mutation":
            return {"success": False, "code": "unsupported_portfolio_proposal_type", "business_effect_applied": False}
        if proposal.status.value != "executing":
            return {"success": False, "code": "proposal_not_executing", "business_effect_applied": False}

        preview, intent = _build_execution_preview(
            proposal,
            user_id=user_id,
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        if not bool(preview.get("success")):
            return {
                "success": False,
                "code": "mutation_preflight_failed",
                "message": str(preview.get("message") or ""),
                "errors": [str(item) for item in preview.get("errors") or []],
                "business_effect_applied": False,
            }

        committed = _plain_result(
            _canonical_commit_handler(
                {
                    "user_id": str(user_id or "default"),
                    "proposal_id": proposal.proposal_id,
                    "intent": intent,
                    "execution_payload": dict(preview.get("execution_payload") or {}),
                },
                {
                    "user_id": str(user_id or "default"),
                    "session_id": str(session_id or ""),
                    "run_id": str(run_id or ""),
                    "output_dir": str(output_dir),
                    "db_path": str(db_path or ""),
                },
            )
        )
        success = bool(committed.get("success"))
        return {
            "success": success,
            "code": "commit_completed" if success else "revalidation_or_commit_failed",
            "message": str(committed.get("message") or ""),
            "data": dict(committed.get("data") or {}),
            "errors": [str(item) for item in committed.get("errors") or []],
            "warnings": [str(item) for item in committed.get("warnings") or []],
            "business_effect_applied": success,
            "execution_ref": {"proposal_id": proposal.proposal_id},
            "canonical_proposal_id": proposal.proposal_id,
            "canonical_proposal_version": proposal.current_version,
        }


__all__ = ["PortfolioMutationAdapter"]
