from __future__ import annotations

import pandas as pd

import ranking_runtime.tushare_features as module


class _FakePro:
    def trade_cal(self, **_kwargs):
        return pd.DataFrame(
            {"cal_date": ["20260807", "20260810"], "is_open": ["1", "1"]}
        )

    def daily_basic(self, *, trade_date: str, fields: str):
        del fields
        return pd.DataFrame(
            {"ts_code": ["000001.SZ"], "trade_date": [trade_date], "close": [10.0]}
        )

    def moneyflow(self, *, trade_date: str, fields: str):
        del fields
        return pd.DataFrame(
            {"ts_code": ["000001.SZ"], "trade_date": [trade_date], "net_mf_amount": [1.0]}
        )

    def index_daily(self, *, ts_code: str, trade_date: str, fields: str):
        del fields
        return pd.DataFrame(
            {"ts_code": [ts_code], "trade_date": [trade_date], "pct_chg": [0.1]}
        )

    def margin_detail(self, *, trade_date: str, fields: str):
        del fields
        return pd.DataFrame(
            {"ts_code": ["000001.SZ"], "trade_date": [trade_date], "rzye": [1.0]}
        )

    def moneyflow_hsgt(self, *, trade_date: str):
        return pd.DataFrame({"trade_date": [trade_date], "north_money": [1.0]})


def test_feature_refresh_fills_every_missing_trade_date_in_one_batch(tmp_path, monkeypatch) -> None:
    feature_dir = tmp_path / "features"
    feature_dir.mkdir()
    pd.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20260807"], "close": [9.9]}
    ).to_csv(feature_dir / "daily_basic.csv", index=False)
    monkeypatch.setattr(module, "init_tushare_pro", lambda _token: _FakePro())

    report = module.refresh_ranker_features_through(
        token="configured-secret",
        signal_date="2026-08-10",
        stock_codes={"000001"},
        feature_dir=feature_dir,
    )

    assert report["ready"] is True
    assert report["endpoints"]["daily_basic"]["requested_dates"] == 1
    assert report["endpoints"]["moneyflow"]["requested_dates"] == 2
    assert report["endpoints"]["daily_basic"]["unresolved_dates"] == []
    dates = pd.read_csv(feature_dir / "daily_basic.csv", dtype=str)["trade_date"].tolist()
    assert dates == ["20260807", "20260810"]
