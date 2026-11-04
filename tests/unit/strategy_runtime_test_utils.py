from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.strategy_config_compiler import StrategyConfigCompiler
from database.connection import initialize_database
from portfolio.paper_account import create_default_account
from portfolio.paper_position import create_position
from strategies.binding_repository import StrategyBindingRepository
from strategies.registry import StrategyManifest, get_strategy_registry
from strategies.runtime_resolver import canonical_config_hash


def runtime_candidates(count: int = 15, price: float = 10.0):
    return [
        {
            "stock_code": f"{rank:06d}",
            "stock_name": f"S{rank}",
            "rank": rank,
            "original_rank": rank,
            "original_score": 1.0 - rank / 100.0,
            "current_price": price,
            "industry": f"I{rank % 5}",
            "risk_level": "low",
            "trade_date": "2026-07-16",
        }
        for rank in range(1, count + 1)
    ]


def runtime_account(user_id: str = "u1"):
    return create_default_account(user_id, 100000.0)


def runtime_position(
    rank: int,
    *,
    user_id: str = "u1",
    weight: float = 0.05,
    price: float = 10.0,
):
    return create_position(
        user_id,
        f"{rank:06d}",
        f"S{rank}",
        quantity=100000.0 * weight / price,
        cost_price=price,
        current_price=price,
        total_assets=100000.0,
        industry=f"I{rank % 5}",
    )


def bind_runtime(
    tmp_path: Path,
    *,
    user_id: str = "u1",
    account_id: str = "paper_u1",
    config: dict[str, Any] | None = None,
    module_path: str = (
        "strategies.adapters.hierarchical_top10_strategy"
    ),
    class_name: str = "HierarchicalTop10Strategy",
    source_type: str = "config_version",
) -> tuple[StrategyManifest, Any]:
    db_path = tmp_path / "agent_quant.db"
    output_dir = tmp_path / "outputs"
    initialize_database(db_path)
    canonical = StrategyConfigCompiler._canonical_config(
        config
        or {
            "entry_top_k": 6,
            "hold_buffer_rank": 12,
            "max_positions": 6,
            "target_invested_weight": 0.60,
            "minimum_cash_ratio": 0.20,
            "min_rebalance_weight_delta": 0.02,
        }
    )
    manifest = StrategyManifest(
        strategy_id="phase6_runtime_strategy",
        strategy_name="Phase 6 runtime strategy",
        version="v1",
        source_type=source_type,
        module_path=module_path,
        class_name=class_name,
        status="registered_disabled",
        validation_status="passed",
        backtest_status="passed",
        metadata={"config": canonical},
    )
    get_strategy_registry(
        output_dir=output_dir,
        db_path=db_path,
    ).register(manifest)
    binding = StrategyBindingRepository(db_path).activate(
        user_id=user_id,
        account_id=account_id,
        strategy_id=manifest.strategy_id,
        strategy_version=manifest.version,
        config_hash=canonical_config_hash(canonical),
        effective_from="2026-01-01",
        source_plan_id="plan_phase6_test",
    )
    return manifest, binding
