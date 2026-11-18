from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ranking_runtime.tushare_features as refresh


# Independent of the downloader constants: these are the indices used by the
# training download script, including 000906.SH rather than the old 000852.SH.
TRAINING_INDICES = (
    "000001.SH", "399001.SZ", "399006.SZ", "000300.SH", "000905.SH", "000906.SH",
)
LEGACY_INDICES = (*TRAINING_INDICES[:-1], "000852.SH")
INDEX_SUFFIXES = (
    "pct_chg", "vol", "amount", "pct_chg_mean_5", "pct_chg_mean_20",
    "pct_chg_mean_60", "pct_chg_volatility_20", "vol_ratio_5", "vol_ratio_20",
    "vol_ratio_60", "amount_ratio_5", "amount_ratio_20", "amount_ratio_60",
)
INDEX_FEATURES = [f"market_index_{code[:6]}_{suffix}" for code in TRAINING_INDICES for suffix in INDEX_SUFFIXES]


def index_frame(code: str, dates: list[str]) -> pd.DataFrame:
    step = np.arange(len(dates), dtype=float)
    volume = 100000.0 + 17 * step + 100 * np.cos(step / 5)
    close = 100.0 + 0.25 * step
    return pd.DataFrame({
        "ts_code": code, "trade_date": dates, "close": close, "open": close - 0.1,
        "high": close + 0.4, "low": close - 0.5, "pre_close": close - 0.2,
        "change": 0.2, "pct_chg": 0.1 + 0.002 * step + 0.03 * np.sin(step / 7),
        "vol": volume, "amount": volume * close,
    })


class IndexProvider:
    def __init__(self, dates: list[str], *, empty_index: bool = False):
        self.dates = dates
        self.empty_index = empty_index
        self.calls: list[tuple[str, str, str]] = []

    def trade_cal(self, *, start_date, end_date, **_kwargs):
        dates = [day for day in self.dates if start_date <= day <= end_date]
        return pd.DataFrame({"cal_date": dates, "is_open": "1"})

    def index_daily(self, *, ts_code, start_date, end_date, fields):
        self.calls.append((ts_code, start_date, end_date))
        assert ts_code == "000906.SH", "Complete legacy index rows must be reused."
        frame = index_frame(ts_code, self.dates)
        if self.empty_index:
            return frame.iloc[:0]
        return frame.loc[frame["trade_date"].between(start_date, end_date), fields.split(",")]

    def daily_basic(self, **_kwargs):
        pytest.fail("Complete daily_basic cache should be reused.")

    def moneyflow(self, **_kwargs):
        pytest.fail("Complete moneyflow cache should be reused.")

    def margin_detail(self, **_kwargs):
        pytest.fail("Complete margin_detail cache should be reused.")

    def moneyflow_hsgt(self, **_kwargs):
        pytest.fail("Complete moneyflow_hsgt cache should be reused.")


@pytest.fixture
def cached_features(tmp_path, monkeypatch):
    monkeypatch.setattr("core.user_scope.current_owner", lambda: "")
    dates = pd.bdate_range(end="2026-11-18", periods=260).strftime("%Y%m%d").tolist()
    codes = ("000001.SZ", "600000.SH", "300001.SZ")
    basic, flow, margin = [], [], []
    for day_index, day in enumerate(dates):
        for stock_index, code in enumerate(codes):
            keys = {"ts_code": code, "trade_date": day}
            basic.append({
                **{column: 2.0 + stock_index + day_index * 0.01 for column in refresh.DAILY_BASIC_FIELDS.split(",")},
                **keys, "close": 10.0 + stock_index + day_index * 0.03,
                "total_mv": 100000.0 + day_index * 100, "circ_mv": 80000.0 + day_index * 80,
            })
            flow.append({
                **{column: 10.0 + day_index * 0.1 for column in refresh.MONEYFLOW_FIELDS.split(",")},
                **keys, "net_mf_amount": 2.0 + day_index * 0.01,
            })
            margin.append({
                **{column: 10.0 + day_index * 0.1 for column in refresh.MARGIN_FIELDS.split(",")},
                **keys,
            })
    pd.DataFrame(basic).to_csv(tmp_path / "daily_basic.csv", index=False)
    pd.DataFrame(flow).to_csv(tmp_path / "moneyflow.csv", index=False)
    pd.DataFrame(margin).to_csv(tmp_path / "margin_detail.csv", index=False)
    pd.DataFrame({"trade_date": dates, "hgt": 20.0, "sgt": 30.0, "north_money": 50.0,
                  "south_money": 40.0}).to_csv(tmp_path / "moneyflow_hsgt.csv", index=False)
    old_indexes = pd.concat([index_frame(code, dates) for code in LEGACY_INDICES], ignore_index=True)
    old_indexes.to_csv(tmp_path / "index_daily.csv", index=False)
    return tmp_path, dates, {code[:6] for code in codes}


@pytest.fixture(scope="module")
def build_panel():
    # Load the real pure-pandas implementation without importing unrelated
    # stock_ranker package exports, which eagerly require torch/lightgbm.
    path = Path(__file__).resolve().parents[2] / "stock_ranker" / "tushare_panel.py"
    spec = importlib.util.spec_from_file_location("contract_test_tushare_panel", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_tushare_panel


def run_refresh(cache, provider, monkeypatch):
    directory, dates, codes = cache
    monkeypatch.setattr(refresh, "init_tushare_pro", lambda _token: provider)
    return refresh.refresh_ranker_features_through(
        token="offline-fixture", signal_date=dates[-1], stock_codes=codes, feature_dir=directory,
    )


def test_legacy_cache_is_repaired_to_the_real_training_panel_contract(cached_features, build_panel, monkeypatch):
    directory, dates, codes = cached_features
    index_path = directory / "index_daily.csv"
    old = pd.read_csv(index_path, dtype={"ts_code": str, "trade_date": str})
    provider = IndexProvider(dates)

    assert run_refresh(cached_features, provider, monkeypatch)["ready"] is True
    additional = ["basic_log_close", "tech_ret_250", "flow_net_ratio", "market_prior_up_rate_60",
                  "margin_total_log", "market_hsgt_north_money_mean_60"]
    panel, features, _ = build_panel(
        directory, start_date="2026-11-18", end_date="2026-11-18",
        required_features=[*INDEX_FEATURES, *additional], source_lookback_trading_days=400,
    )

    assert len(panel) == len(codes)
    assert set(features) == set(INDEX_FEATURES + additional)
    assert np.isfinite(panel[features].to_numpy()).all()
    assert (panel[INDEX_FEATURES].to_numpy() != 0).all()
    expected = index_frame("000906.SH", dates)
    assert np.allclose(panel["market_index_000906_pct_chg"], expected["pct_chg"].iloc[-1])
    assert np.allclose(panel["market_index_000906_pct_chg_mean_60"], expected["pct_chg"].tail(60).mean())
    assert np.allclose(panel["market_index_000906_vol_ratio_20"], expected["vol"].iloc[-1] / expected["vol"].tail(20).mean())

    assert provider.calls and {call[0] for call in provider.calls} == {"000906.SH"}
    assert len(provider.calls) == 2  # 260 trading days in bounded ranges of 250.
    updated = pd.read_csv(index_path, dtype={"ts_code": str, "trade_date": str})
    assert set(updated["ts_code"]) == {*LEGACY_INDICES, "000906.SH"}
    assert not updated.duplicated(["ts_code", "trade_date"]).any()
    pd.testing.assert_frame_equal(
        updated.loc[updated["ts_code"].isin(LEGACY_INDICES)].sort_values(["ts_code", "trade_date"]).reset_index(drop=True),
        old.sort_values(["ts_code", "trade_date"]).reset_index(drop=True),
    )
    provider.calls.clear()
    assert run_refresh(cached_features, provider, monkeypatch)["ready"] is True
    assert provider.calls == []


def test_missing_training_index_keeps_refresh_unready(cached_features, build_panel, monkeypatch):
    directory, dates, _ = cached_features
    path = directory / "index_daily.csv"
    before = path.read_bytes()
    provider = IndexProvider(dates, empty_index=True)

    report = run_refresh(cached_features, provider, monkeypatch)

    assert report["ready"] is False
    assert report["endpoints"]["index_daily"]["unresolved_dates"] == dates
    assert {call[0] for call in provider.calls} == {"000906.SH"}
    assert path.read_bytes() == before
    with pytest.raises(RuntimeError, match="market_index_000906"):
        build_panel(directory, start_date="2026-11-18", end_date="2026-11-18",
                    required_features=INDEX_FEATURES, source_lookback_trading_days=400)
