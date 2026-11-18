from __future__ import annotations

import pandas as pd
import pytest

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

    def index_daily(self, *, ts_code: str, start_date: str, end_date: str, fields: str):
        del fields
        dates = [day for day in ["20260807", "20260810"] if start_date <= day <= end_date]
        return pd.DataFrame(
            {"ts_code": [ts_code] * len(dates), "trade_date": dates, "pct_chg": [0.1] * len(dates)}
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


class _RangePro:
    def __init__(self, dates):
        self.dates = dates
        self.calls = []

    def index_daily(self, *, ts_code, start_date, end_date, fields):
        self.calls.append((ts_code, start_date, end_date))
        dates = [day for day in self.dates if start_date <= day <= end_date]
        return pd.DataFrame({"ts_code": [ts_code] * len(dates), "trade_date": dates, "close": 100.0})


def _refresh_indexes(pro, path, dates):
    report = {"ready": True, "endpoints": {}}
    module._fetch_missing_index_dates(pro=pro, path=path, trade_dates=dates, report=report)
    return report


def test_index_history_uses_bounded_ranges_and_reuses_complete_cache(tmp_path):
    dates = pd.bdate_range("2025-08-01", periods=297).strftime("%Y%m%d").tolist()
    pro = _RangePro(dates)
    path = tmp_path / "index_daily.csv"
    assert _refresh_indexes(pro, path, dates)["ready"]
    assert len(pro.calls) == 12  # 6 indices × 2 ranges, rather than 6 × 297 requests.
    for _, start, end in pro.calls:
        assert sum(start <= day <= end for day in dates) <= 250
    cached = pd.read_csv(path, dtype=str)
    assert len(cached) == len(dates) * 6
    assert not cached.duplicated(["ts_code", "trade_date"]).any()
    pro.calls.clear()
    assert _refresh_indexes(pro, path, dates)["endpoints"]["index_daily"]["cached"]
    assert pro.calls == []
    # Reproduce a failed first refresh: 244 complete dates and 53 scattered gaps.
    gaps = set(dates[80:107] + dates[-26:])
    cached.loc[~cached["trade_date"].isin(gaps)].to_csv(path, index=False)
    report = _refresh_indexes(pro, path, dates)
    assert report["ready"]
    assert report["endpoints"]["index_daily"]["requested_dates"] == 53
    assert report["endpoints"]["index_daily"]["rows"] == 53 * 6
    assert len(pro.calls) == 12
    assert len(pd.read_csv(path)) == len(dates) * 6


def test_index_cache_requires_every_index_and_ignores_duplicate_or_unexpected_codes(tmp_path):
    dates = ["20260807", "20260810"]
    missing_code = module.INDEX_CODES[-1]
    records = [{"ts_code": code, "trade_date": day, "close": 100.0}
               for day in dates for code in module.INDEX_CODES
               if (code, day) != (missing_code, dates[0])]
    records += [records[0], {"ts_code": "unexpected", "trade_date": dates[0], "close": 100.0}]
    path = tmp_path / "index_daily.csv"
    pd.DataFrame(records).to_csv(path, index=False)
    pro = _RangePro(dates)
    report = _refresh_indexes(pro, path, dates)
    assert report["ready"]
    assert pro.calls == [(missing_code, dates[0], dates[0])]
    assert report["endpoints"]["index_daily"]["requested_dates"] == 1


def test_index_failure_preserves_successes_and_next_run_only_fetches_missing_index(tmp_path):
    dates = ["20260807", "20260810"]
    failing_code = module.INDEX_CODES[1]

    class FailingPro(_RangePro):
        def index_daily(self, **kwargs):
            if kwargs["ts_code"] == failing_code:
                raise Exception("provider failed token=synthetic-secret https://private.example/path")
            return super().index_daily(**kwargs)

    path = tmp_path / "index_daily.csv"
    report = _refresh_indexes(FailingPro(dates), path, dates)
    assert not report["ready"]
    assert report["endpoints"]["index_daily"]["unresolved_dates"] == dates
    assert len(pd.read_csv(path)) == 5 * len(dates)
    assert "synthetic-secret" not in str(report)
    assert "private.example" not in str(report)
    assert report["endpoints"]["index_daily"]["errors"][0]["error_category"] == "request_failed"
    retry = _RangePro(dates)
    assert _refresh_indexes(retry, path, dates)["ready"]
    assert retry.calls == [(failing_code, dates[0], dates[-1])]


@pytest.mark.parametrize("error,delays,category", [
    (Exception("每分钟最多访问此接口500次，权限详情：https://example.invalid/limits"), [60, 60], "rate_limited"),
    (TimeoutError("connection timed out"), [1, 2], "temporarily_unavailable"),
    (module.RequestsTimeout(), [1, 2], "temporarily_unavailable"),
    (Exception("您没有访问该接口的权限"), [], "permission_denied"),
    (Exception("每天最多访问此接口10000次"), [], "quota_exceeded"),
    (Exception("您的token不对，请确认"), [], "invalid_token"),
    (Exception("unknown provider failure"), [], "request_failed"),
])
def test_index_retries_only_bounded_transient_failures(monkeypatch, error, delays, category):
    sleeps = []
    calls = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    class Pro:
        def index_daily(self, **kwargs):
            calls.append(kwargs)
            raise error

    with pytest.raises(type(error)):
        module._request_index_range(Pro(), code=module.INDEX_CODES[0], start="20260807", end="20260810")
    assert sleeps == delays
    assert len(calls) == len(delays) + 1
    assert module._request_error_category(error) == category


def test_index_rate_limit_retry_can_succeed(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    class OnceLimited(_RangePro):
        limited = False

        def index_daily(self, **kwargs):
            if not self.limited:
                self.limited = True
                raise Exception("每分钟最多访问此接口500次")
            return super().index_daily(**kwargs)

    dates = ["20260807", "20260810"]
    assert _refresh_indexes(OnceLimited(dates), tmp_path / "index_daily.csv", dates)["ready"]
    assert sleeps == [60]


def test_nonempty_but_incomplete_index_response_stays_unready(tmp_path):
    dates = ["20260807", "20260810"]

    class IncompletePro(_RangePro):
        def index_daily(self, **kwargs):
            frame = super().index_daily(**kwargs)
            if kwargs["ts_code"] == module.INDEX_CODES[0]:
                frame.loc[frame["trade_date"].eq(dates[-1]), "ts_code"] = "unexpected"
            # An out-of-range response must not count as one of the requested dates.
            return pd.concat([frame, pd.DataFrame([{"ts_code": kwargs["ts_code"], "trade_date": "20260811", "close": 100.0}])])

    report = _refresh_indexes(IncompletePro(dates), tmp_path / "index_daily.csv", dates)
    assert report["ready"] is False
    assert report["endpoints"]["index_daily"]["unresolved_dates"] == [dates[-1]]
    assert "index_daily 缺少 1 个交易日" in module.summarize_feature_refresh_failure(report)


def test_existing_main_caches_do_not_shrink_required_history_to_ninety_days(tmp_path):
    for name in ("daily_basic", "moneyflow"):
        pd.DataFrame({"trade_date": ["20261117"], "ts_code": ["000001.SZ"]}).to_csv(tmp_path / f"{name}.csv", index=False)
    requests = []

    class Pro:
        def trade_cal(self, **kwargs):
            requests.append(kwargs)
            return pd.DataFrame({"cal_date": ["20250825", "20261117"], "is_open": [1, 1]})

    module._open_dates_for_catchup(Pro(), tmp_path, "20261117")
    assert requests[0]["start_date"] == "20250824"


def test_failure_summary_names_missing_required_endpoint_without_raw_provider_errors():
    report = {"endpoints": {
        "index_daily": {"required": True, "unresolved_dates": ["20260807", "20260810"],
                        "errors": [{"error_category": "rate_limited", "message": "token=synthetic-secret"}]},
        "moneyflow_hsgt": {"required": False, "unresolved_dates": ["20260807"]},
    }}
    message = module.summarize_feature_refresh_failure(report)
    assert "index_daily 缺少 2 个交易日" in message
    assert "接口限频" in message
    assert "moneyflow_hsgt" not in message
    assert "synthetic-secret" not in message
