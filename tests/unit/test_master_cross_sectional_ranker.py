from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pandas as pd
import torch

from stock_ranker import MasterRanker


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "model_precision" / "experiment_master_top15.py"
SPEC = importlib.util.spec_from_file_location("experiment_master_top15", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_master_outputs_one_cross_sectional_score_per_stock() -> None:
    model = MasterRanker(
        stock_feature_count=6,
        market_feature_count=3,
        model_dimension=8,
        temporal_heads=2,
        stock_heads=2,
        maximum_lookback=8,
    )

    scores = model(torch.randn(17, 8, 9))

    assert scores.shape == (17,)
    assert torch.isfinite(scores).all()


def test_feature_contract_excludes_every_kronos_lineage_column() -> None:
    stock, market = MODULE.select_feature_columns(
        [
            "pred_return",
            "kronos_rank_pct",
            "stock_direction_accuracy_20",
            "basic_pb_pct",
            "flow_net_ratio",
            "cross_pct_flow_net_ratio",
            "market_index_000300_pct_chg",
        ]
    )

    assert stock == [
        "basic_pb_pct",
        "cross_pct_flow_net_ratio",
        "flow_net_ratio",
    ]
    assert market == ["market_index_000300_pct_chg"]


def test_top15_metrics_require_exactly_fifteen_stocks_every_day() -> None:
    complete = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-05"] * 15 + ["2026-01-06"] * 15),
            "code": [f"{index:06d}" for index in range(30)],
            "master_score": list(range(30)),
            "label_up_tushare": [1] * 9 + [0] * 6 + [1] * 6 + [0] * 9,
        }
    )
    incomplete = complete.iloc[:-1].copy()

    assert MODULE.top15_metrics(complete)["precision"] == 0.5
    assert MODULE.top15_metrics(complete)["all_days_have_15"] is True
    assert MODULE.top15_metrics(incomplete)["all_days_have_15"] is False


def test_pairwise_losses_are_finite_and_differentiable() -> None:
    scores = torch.tensor([-0.2, 0.1, 0.4, -0.3], requires_grad=True)
    returns = torch.tensor([-0.01, 0.02, 0.03, -0.02])
    labels = torch.tensor([0.0, 1.0, 1.0, 0.0])

    for kind in ("pairwise", "rank_bce"):
        loss = MODULE._loss(scores, returns, labels, kind)
        assert torch.isfinite(loss)
        loss.backward(retain_graph=True)

    assert scores.grad is not None
    assert torch.isfinite(scores.grad).all()
