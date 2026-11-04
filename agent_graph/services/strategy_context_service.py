from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.strategy_proposal_service import (
    StrategyConversationContext,
    StrategyProposalService,
)
from database.repositories import AgentRepository
from portfolio.paper_strategy_config import default_paper_strategy_config
from portfolio.storage import PortfolioStorage
from portfolio.user_profile import load_user_context
from strategies.adapters.hierarchical_top10_strategy import (
    HierarchicalTop10Strategy,
)
from strategies.runtime_resolver import StrategyRuntimeResolver


class StrategyContextService:
    """Build the factual context consumed by the LLM strategy conversation."""

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        output_dir: str | Path = "outputs",
    ) -> None:
        self.db_path = db_path
        self.output_dir = Path(output_dir)
        self.proposals = StrategyProposalService(db_path)

    def load(
        self,
        *,
        user_id: str,
        account_id: str,
        conversation_id: str,
    ) -> StrategyConversationContext:
        portfolio_dir = (
            self.output_dir / "portfolio" / str(user_id or "default")
        )
        storage = PortfolioStorage(
            self.db_path,
            output_dir=portfolio_dir,
            use_database=True,
        )
        account = storage.load_account(account_id)
        positions = storage.load_positions(user_id)
        strategy = HierarchicalTop10Strategy()
        try:
            runtime = StrategyRuntimeResolver(
                db_path=self.db_path,
                output_dir=self.output_dir,
            ).resolve(
                user_id=user_id,
                account_id=account_id,
            )
            runtime_config = runtime.resolved_config()
            current_strategy = {
                "strategy_id": runtime.strategy_id,
                "strategy_version": runtime.strategy_version,
                "binding_id": runtime.binding_id,
                "config_hash": runtime.config_hash,
                "runtime_mode": (
                    "hierarchical_top10"
                    if runtime.module_path
                    == "strategies.adapters.hierarchical_top10_strategy"
                    else "runtime_plugin"
                ),
                "config": runtime_config,
                "registry_currently_consumed_by_pipeline": True,
            }
        except Exception:
            config = default_paper_strategy_config(user_id).to_dict()
            current_strategy = {
                "strategy_id": strategy.strategy_id,
                "strategy_version": strategy.version,
                "binding_id": "",
                "config_hash": "",
                "runtime_mode": "hierarchical_top10",
                "config": {
                    "entry_top_k": int(config.get("entry_top_k") or 10),
                    "hold_buffer_rank": int(
                        config.get("hold_buffer_rank") or 15
                    ),
                    "max_positions": int(
                        config.get("max_positions") or 10
                    ),
                    "target_invested_weight": float(
                        config.get("target_invested_weight") or 0.80
                    ),
                    "minimum_cash_ratio": float(
                        config.get("minimum_cash_ratio")
                        or config.get("target_cash_ratio")
                        or 0.05
                    ),
                    "min_rebalance_weight_delta": float(
                        config.get("min_rebalance_weight_delta")
                        or 0.01
                    ),
                },
                "registry_currently_consumed_by_pipeline": True,
            }
        try:
            _, _, _, constraints = load_user_context(
                user_id,
                db_path=self.db_path,
                output_dir=self.output_dir,
            )
        except Exception:
            constraints = {}

        conversation: list[dict[str, Any]] = []
        if conversation_id:
            try:
                repo = AgentRepository(self.db_path)
                row = repo.get_conversation(conversation_id)
                if row and str(row.get("user_id") or "") == str(user_id):
                    conversation = [
                        {
                            "role": message.get("role"),
                            "content": str(message.get("content") or "")[:1200],
                            "created_at": message.get("created_at"),
                        }
                        for message in repo.list_messages(
                            conversation_id,
                            limit=50,
                        )
                        if str(message.get("user_id") or "") == str(user_id)
                    ]
            except Exception:
                conversation = []

        active = self.proposals.get_active(
            user_id=user_id,
            account_id=account_id,
            conversation_id=conversation_id,
        )
        versions = (
            self.proposals.list_versions(
                active.proposal_id,
                user_id=user_id,
            )
            if active
            else []
        )
        return StrategyConversationContext(
            user_id=user_id,
            account_id=account_id,
            conversation_id=conversation_id,
            current_account=account.to_dict() if account else {},
            current_positions=[
                position.to_dict() for position in positions
            ],
            current_strategy=current_strategy,
            strategy_capabilities={
                "base_strategy": strategy.strategy_id,
                "config_schema": strategy.get_config_schema(),
                "supports_config_only": [
                    "entry_top_k",
                    "hold_buffer_rank",
                    "max_positions",
                    "target_invested_weight",
                    "minimum_cash_ratio",
                    "min_rebalance_weight_delta",
                ],
            },
            user_constraints=dict(constraints or {}),
            related_conversation=conversation,
            active_proposal=active.to_dict() if active else {},
            proposal_version_history=[
                {
                    "version": item.version,
                    "change_summary": item.change_summary,
                    "user_feedback": item.user_feedback,
                    "proposal_json": item.proposal_json,
                    "created_at": item.created_at,
                }
                for item in versions
            ],
        )
