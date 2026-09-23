from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_gm import (
    build_gm_daily_feature_panel,
    normalize_gm_bars,
    normalize_gm_symbol,
    select_signal_date,
)
from gm_daily_ranker import GmDailyRanker
from tools.export_gm_targets import export_targets_from_frame


def _raw_frame(symbol: str, dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "eob": pd.to_datetime(dates).astype(str),
            "open": np.linspace(10, 11, len(dates)),
            "high": np.linspace(10.5, 11.5, len(dates)),
            "low": np.linspace(9.5, 10.5, len(dates)),
            "close": np.linspace(10, 11, len(dates)),
            "volume": np.linspace(1_000_000, 1_100_000, len(dates)),
            "amount": np.linspace(10_000_000, 11_000_000, len(dates)),
        }
    )


def test_normalize_gm_symbol_formats_a_share_codes() -> None:
    assert normalize_gm_symbol("600519") == "SHSE.600519"
    assert normalize_gm_symbol("000858") == "SZSE.000858"
    assert normalize_gm_symbol("SHSE.688001") == "SHSE.688001"
    assert normalize_gm_symbol("bad") == ""


def test_normalize_gm_bars_and_select_latest_complete_date() -> None:
    dates = ["2026-01-05", "2026-01-06", "2026-01-07"]
    bars = pd.concat(
        [
            normalize_gm_bars(_raw_frame("SHSE.600519", dates), symbol="SHSE.600519"),
            normalize_gm_bars(_raw_frame("SZSE.000858", dates), symbol="SZSE.000858"),
        ],
        ignore_index=True,
    )
    assert bars.loc[0, "code"] == "600519"
    assert bars.iloc[-1]["date"] == "2026-01-07"
    assert select_signal_date(bars, min_coverage=1.0) == "2026-01-07"


def test_build_gm_daily_feature_panel_has_fixed_columns() -> None:
    dates = pd.bdate_range("2026-01-01", periods=70).strftime("%Y-%m-%d").tolist()
    frames = []
    for offset, symbol in enumerate(("SHSE.600519", "SZSE.000858")):
        raw = _raw_frame(symbol, dates)
        raw["close"] = raw["close"] + offset
        frames.append(normalize_gm_bars(raw, symbol=symbol))
    bars = pd.concat(frames, ignore_index=True)
    signal_date = dates[-1]
    panel = build_gm_daily_feature_panel(bars, signal_date=signal_date)
    assert len(panel) == 2
    assert {"date", "code", "symbol", "close_log", "ret_20", "volatility_20"}.issubset(panel.columns)


def test_single_ranker_and_target_export(tmp_path: Path) -> None:
    class FakeBooster:
        def feature_name(self):
            return [
                "close_log", "ret_1", "ret_5", "ret_10", "ret_20", "ret_60",
                "close_to_ma_5", "close_to_ma_20", "close_to_ma_60",
                "volatility_5", "volatility_20", "volatility_60",
                "drawdown_20", "volume_ratio_5_20", "amount_ratio_5_20", "amount_log",
            ]

        def predict(self, features):
            return np.linspace(0.1, 0.2, len(features))

    dates = pd.bdate_range("2026-01-01", periods=70).strftime("%Y-%m-%d").tolist()
    bars = pd.concat(
        [normalize_gm_bars(_raw_frame(symbol, dates), symbol=symbol) for symbol in ("SHSE.600519", "SZSE.000858")],
        ignore_index=True,
    )
    signal_date = dates[-1]
    panel = build_gm_daily_feature_panel(bars, signal_date=signal_date)

    model_path = tmp_path / "model.txt"
    model_path.write_text("fake", encoding="utf-8")
    ranker = GmDailyRanker.__new__(GmDailyRanker)
    ranker.model = FakeBooster()
    ranker.model_path = model_path
    ranker.model_version = "test123"
    ranked = ranker.predict(panel)
    ranked["signal_date"] = signal_date
    assert ranked["pred_rank"].tolist() == [1, 2]

    output = tmp_path / "targets.csv"
    _, targets = export_targets_from_frame(
        ranked,
        top_k=1,
        cash_ratio=0.98,
        trade_time="10:00:00",
        output=output,
    )
    assert len(targets) == 1
    assert targets.iloc[0]["target_weight"] == pytest.approx(0.98)
