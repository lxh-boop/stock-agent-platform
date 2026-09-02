from __future__ import annotations

import pandas as pd

from application.web_paper_trading_service import WebPaperTradingApplicationService
from application.web_read_service import web_read_service
from portfolio.paper_market_data import load_paper_ohlc_for_date, merge_paper_market_ohlc


def test_paper_market_cache_round_trip_and_replaces_same_day_code(tmp_path) -> None:
    cache = tmp_path / "paper_market_ohlc.csv"
    first = pd.DataFrame([
        {
            "ts_code": "600004.SH",
            "trade_date": "20260806",
            "open": 7.70,
            "high": 7.74,
            "low": 7.61,
            "close": 7.66,
        }
    ])
    report = merge_paper_market_ohlc(first, source="tushare_daily", cache_path=cache)
    assert report["rows_after"] == 1

    selected = load_paper_ohlc_for_date("2026-08-06", cache_path=cache)
    assert selected.to_dict(orient="records") == [
        {
            "code": "600004",
            "date": "2026-08-06",
            "open": 7.70,
            "high": 7.74,
            "low": 7.61,
            "close": 7.66,
            "source": "tushare_daily",
        }
    ]

    corrected = first.assign(high=7.75)
    merge_paper_market_ohlc(corrected, source="correction", cache_path=cache)
    selected = load_paper_ohlc_for_date("20260806", cache_path=cache)
    assert len(selected) == 1
    assert selected.iloc[0]["high"] == 7.75
    assert selected.iloc[0]["source"] == "correction"


def test_paper_history_uses_full_cached_ohlc_before_close_only_fallback(monkeypatch) -> None:
    monkeypatch.setattr(web_read_service, "load_signal_ohlc_data", lambda: pd.DataFrame())
    monkeypatch.setattr(
        "portfolio.paper_market_data.load_paper_ohlc_for_date",
        lambda _date: pd.DataFrame([
            {
                "code": "600004",
                "date": "2026-08-06",
                "open": 7.70,
                "high": 7.74,
                "low": 7.61,
                "close": 7.66,
                "source": "tushare_daily",
            }
        ]),
    )
    monkeypatch.setattr(
        "application.support.ranker_market_data.load_ranker_close_for_date",
        lambda _date: pd.DataFrame(columns=["code", "date", "close"]),
    )
    orders = pd.DataFrame([{"stock_code": "600004", "executed_price": 7.66}])

    result = WebPaperTradingApplicationService()._attach_daily_ohlc(
        orders,
        "2026-08-06",
    ).iloc[0]

    assert [result[column] for column in ("open", "high", "low", "close")] == [
        7.70,
        7.74,
        7.61,
        7.66,
    ]
    assert bool(result["ohlc_available"]) is True
    assert bool(result["close_available"]) is True
    assert result["market_data_status"] == "完整OHLC"
    assert result["market_data_source"] == "tushare_daily"
