from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any

from agent_graph.services.strategy_config_compiler import (
    CANONICAL_CONFIG_DEFAULTS,
    StrategyConfigCompiler,
)
from strategies.adapters.hierarchical_top10_strategy import (
    HierarchicalTop10Strategy,
)
from strategies.binding_repository import StrategyBindingRepository
from strategies.registry import get_strategy_registry
from strategies.base import PortfolioStrategy, StrategyContext, StrategyResult


def canonical_config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class CanonicalRuntimeStrategy:
    strategy_id: str
    strategy_version: str
    binding_id: str
    config_hash: str
    entry_top_k: int
    hold_buffer_rank: int
    max_positions: int
    target_invested_weight: float
    minimum_cash_ratio: float
    min_rebalance_weight_delta: float
    source: str
    module_path: str
    class_name: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def resolved_config(self) -> dict[str, Any]:
        return {
            "entry_top_k": self.entry_top_k,
            "hold_buffer_rank": self.hold_buffer_rank,
            "max_positions": self.max_positions,
            "target_invested_weight": self.target_invested_weight,
            "minimum_cash_ratio": self.minimum_cash_ratio,
            "min_rebalance_weight_delta": (
                self.min_rebalance_weight_delta
            ),
        }


class StrategyRuntimeResolver:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        output_dir: str | Path = "outputs",
    ) -> None:
        self.bindings = StrategyBindingRepository(db_path)
        self.registry = get_strategy_registry(
            output_dir=output_dir,
            db_path=db_path,
        )

    def resolve(
        self,
        *,
        user_id: str,
        account_id: str,
        as_of_date: str | None = None,
    ) -> CanonicalRuntimeStrategy:
        binding = self.bindings.get_effective(
            user_id=user_id,
            account_id=account_id,
            as_of_date=as_of_date,
        )
        if binding is None:
            strategy = HierarchicalTop10Strategy()
            config = dict(CANONICAL_CONFIG_DEFAULTS)
            return self._result(
                strategy_id=strategy.strategy_id,
                strategy_version=strategy.version,
                binding_id="",
                config=config,
                source="builtin_default",
                module_path=(
                    "strategies.adapters.hierarchical_top10_strategy"
                ),
                class_name="HierarchicalTop10Strategy",
            )
        manifest = self.registry.get(
            binding.strategy_id,
            binding.strategy_version,
        )
        if manifest is None:
            raise ValueError("bound_strategy_version_not_found")
        raw_config = dict(manifest.metadata.get("config") or {})
        config = StrategyConfigCompiler._canonical_config(raw_config)
        actual_hash = canonical_config_hash(config)
        if actual_hash != binding.config_hash:
            raise ValueError("binding_config_hash_mismatch")
        return self._result(
            strategy_id=manifest.strategy_id,
            strategy_version=manifest.version,
            binding_id=binding.binding_id,
            config=config,
            source="account_binding",
            module_path=manifest.module_path,
            class_name=manifest.class_name,
        )

    @staticmethod
    def load_strategy(
        runtime: CanonicalRuntimeStrategy,
    ) -> PortfolioStrategy:
        module = importlib.import_module(runtime.module_path)
        strategy_class = getattr(module, runtime.class_name)
        strategy = strategy_class()
        if not isinstance(strategy, PortfolioStrategy):
            raise TypeError("resolved_strategy_is_not_portfolio_strategy")
        return strategy

    def generate_target(
        self,
        runtime: CanonicalRuntimeStrategy,
        context: StrategyContext,
    ) -> StrategyResult:
        strategy = self.load_strategy(runtime)
        errors = strategy.validate_config(runtime.resolved_config())
        if errors:
            raise ValueError(
                "resolved_strategy_config_invalid:" + ",".join(errors)
            )
        return strategy.generate_target(
            context,
            runtime.resolved_config(),
        )

    def with_config(
        self,
        runtime: CanonicalRuntimeStrategy,
        config: dict[str, Any],
        *,
        source: str = "legacy_runtime_override",
    ) -> CanonicalRuntimeStrategy:
        return self._result(
            strategy_id=runtime.strategy_id,
            strategy_version=runtime.strategy_version,
            binding_id=runtime.binding_id,
            config=config,
            source=source,
            module_path=runtime.module_path,
            class_name=runtime.class_name,
        )

    @staticmethod
    def _result(
        *,
        strategy_id: str,
        strategy_version: str,
        binding_id: str,
        config: dict[str, Any],
        source: str,
        module_path: str,
        class_name: str,
    ) -> CanonicalRuntimeStrategy:
        canonical = StrategyConfigCompiler._canonical_config(config)
        return CanonicalRuntimeStrategy(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            binding_id=binding_id,
            config_hash=canonical_config_hash(canonical),
            entry_top_k=int(canonical["entry_top_k"]),
            hold_buffer_rank=int(canonical["hold_buffer_rank"]),
            max_positions=int(canonical["max_positions"]),
            target_invested_weight=float(
                canonical["target_invested_weight"]
            ),
            minimum_cash_ratio=float(canonical["minimum_cash_ratio"]),
            min_rebalance_weight_delta=float(
                canonical["min_rebalance_weight_delta"]
            ),
            source=source,
            module_path=module_path,
            class_name=class_name,
        )
