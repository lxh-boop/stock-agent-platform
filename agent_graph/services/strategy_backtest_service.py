from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any

from agent_graph.services.strategy_config_compiler import (
    CANONICAL_CONFIG_DEFAULTS,
)


BACKTEST_MARKET_RETURNS = [
    0.004,
    -0.006,
    0.008,
    0.003,
    -0.012,
    0.006,
    0.002,
    -0.004,
    0.009,
    -0.003,
    0.005,
    -0.007,
    0.011,
    0.002,
    -0.005,
    0.007,
    0.001,
    -0.002,
    0.006,
    -0.004,
]
BACKTEST_DATES = [f"2026-05-{day:02d}" for day in range(1, 21)]
BACKTEST_FEE_RATE = 0.0003


@dataclass(frozen=True)
class StrategyBacktestMetrics:
    annualized_return: float
    max_drawdown: float
    volatility: float
    turnover: float
    average_cash_ratio: float
    concentration: float
    executability_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyBacktestService:
    """Deterministic isolated comparison using identical dates and costs."""

    def run(self, candidate_config: dict[str, Any]) -> dict[str, Any]:
        inputs = {
            "dates": BACKTEST_DATES,
            "market_returns": BACKTEST_MARKET_RETURNS,
            "fee_rate": BACKTEST_FEE_RATE,
            "ranking_policy": "fixed_rank_1_to_20",
            "initial_assets": 100000.0,
        }
        input_hash = hashlib.sha256(
            json.dumps(inputs, sort_keys=True).encode("utf-8")
        ).hexdigest()
        baseline_config = dict(CANONICAL_CONFIG_DEFAULTS)
        baseline = self._metrics(baseline_config)
        candidate = self._metrics(
            {**baseline_config, **dict(candidate_config or {})}
        )
        return {
            "status": "passed",
            "isolation": {
                "uses_temporary_account": True,
                "writes_formal_account": False,
                "writes_formal_outputs": False,
            },
            "input_hash": input_hash,
            "baseline_input_hash": input_hash,
            "candidate_input_hash": input_hash,
            "inputs": inputs,
            "baseline_config": baseline_config,
            "candidate_config": {
                **baseline_config,
                **dict(candidate_config or {}),
            },
            "baseline_metrics": baseline.to_dict(),
            "candidate_metrics": candidate.to_dict(),
            "tradeoffs": self._tradeoffs(baseline, candidate),
        }

    @staticmethod
    def _metrics(config: dict[str, Any]) -> StrategyBacktestMetrics:
        exposure = max(
            0.0,
            min(1.0, float(config.get("target_invested_weight") or 0.0)),
        )
        min_cash = max(
            0.0,
            min(1.0, float(config.get("minimum_cash_ratio") or 0.0)),
        )
        exposure = min(exposure, 1.0 - min_cash)
        positions = max(1, int(config.get("max_positions") or 1))
        threshold = max(
            0.0,
            float(config.get("min_rebalance_weight_delta") or 0.0),
        )
        buffer_rank = max(1, int(config.get("hold_buffer_rank") or 1))
        entry_rank = max(1, int(config.get("entry_top_k") or 1))
        turnover = max(
            0.02,
            min(
                1.0,
                0.45
                - min(0.25, threshold * 5.0)
                - min(0.15, max(0, buffer_rank - entry_rank) * 0.01),
            ),
        )
        daily = [
            exposure * market_return
            - turnover * BACKTEST_FEE_RATE / len(BACKTEST_MARKET_RETURNS)
            for market_return in BACKTEST_MARKET_RETURNS
        ]
        nav = 1.0
        peak = 1.0
        drawdown = 0.0
        for value in daily:
            nav *= 1.0 + value
            peak = max(peak, nav)
            drawdown = min(drawdown, nav / peak - 1.0)
        mean = sum(daily) / len(daily)
        variance = sum((value - mean) ** 2 for value in daily) / len(daily)
        annualized = nav ** (252.0 / len(daily)) - 1.0
        volatility = math.sqrt(variance) * math.sqrt(252.0)
        return StrategyBacktestMetrics(
            annualized_return=round(annualized, 8),
            max_drawdown=round(drawdown, 8),
            volatility=round(volatility, 8),
            turnover=round(turnover, 8),
            average_cash_ratio=round(1.0 - exposure, 8),
            concentration=round(exposure / positions, 8),
            executability_rate=1.0 if positions <= 20 else 0.95,
        )

    @staticmethod
    def _tradeoffs(
        baseline: StrategyBacktestMetrics,
        candidate: StrategyBacktestMetrics,
    ) -> list[dict[str, Any]]:
        labels = {
            "annualized_return": "年化收益",
            "max_drawdown": "最大回撤",
            "volatility": "波动率",
            "turnover": "换手率",
            "average_cash_ratio": "现金比例",
            "concentration": "集中度",
            "executability_rate": "可执行率",
        }
        baseline_data = baseline.to_dict()
        candidate_data = candidate.to_dict()
        return [
            {
                "metric": key,
                "label": label,
                "baseline": baseline_data[key],
                "candidate": candidate_data[key],
                "delta": round(candidate_data[key] - baseline_data[key], 8),
            }
            for key, label in labels.items()
        ]
