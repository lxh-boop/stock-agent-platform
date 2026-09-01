from __future__ import annotations

import pandas as pd

from application.web_read_service import WebReadApplicationService
from server.api.main import create_app
from server.api.presenters.common import table_payload, to_browser_value
from server.api.presenters.dashboard import present_ranking_page


EXPECTED_READ_ONLY_PATHS = {
    "/api/v1/web/dashboard/summary",
    "/api/v1/web/dashboard/rankings",
    "/api/v1/web/dashboard/model-status",
    "/api/v1/web/dashboard/data-freshness",
    "/api/v1/web/stocks/{stock_code}",
    "/api/v1/web/stocks/{stock_code}/history",
    "/api/v1/web/stocks/{stock_code}/evidence",
    "/api/v1/web/stocks/{stock_code}/explanation",
    "/api/v1/web/models/metrics",
    "/api/v1/web/models/catalog",
    "/api/v1/web/models/search-results",
    "/api/v1/web/backtests",
    "/api/v1/web/backtests/{backtest_id}",
    "/api/v1/web/backtests/{backtest_id}/equity",
    "/api/v1/web/backtests/{backtest_id}/trades",
    "/api/v1/web/backtests/{backtest_id}/predictions",
    "/api/v1/web/news/events",
    "/api/v1/web/news/events/{event_id}",
    "/api/v1/web/settings",
    "/api/v1/web/monitor/summary",
    "/api/v1/web/monitor/services",
    "/api/v1/web/monitor/history",
    "/api/v1/web/monitor/alerts",
}
HTTP_METHODS = {"get", "head", "post", "put", "patch", "delete", "options", "trace"}


def test_stage6_2_web_routes_keep_read_contract_with_settings_write_extension() -> None:
    # Verify the public OpenAPI contract instead of FastAPI/Starlette internal
    # app.routes objects. Some supported versions keep included routers as
    # _IncludedRouter bookkeeping objects without a direct ``path`` attribute.
    paths = create_app().openapi().get("paths", {})
    web_paths = {
        str(path): path_item
        for path, path_item in paths.items()
        if str(path).startswith("/api/v1/web")
    }

    missing = EXPECTED_READ_ONLY_PATHS - set(web_paths)
    assert not missing, f"missing stage 6.2 OpenAPI paths: {sorted(missing)}"

    for path in sorted(EXPECTED_READ_ONLY_PATHS):
        path_item = web_paths[path]
        methods = {str(key).lower() for key in path_item if str(key).lower() in HTTP_METHODS}
        assert methods, f"no HTTP method found for {path}"
        if path == "/api/v1/web/settings":
            assert methods == {"get", "put"}, methods
        else:
            assert methods <= {"get", "head"}, f"write method exposed by {path}: {sorted(methods)}"


def test_browser_presenter_removes_secrets_and_paths() -> None:
    value = to_browser_value({
        "stock_code": "000001",
        "api_key": "secret",
        "confirmation_token": "token",
        "db_path": r"D:\stock_daily_app\agent.db",
        "message": r"read D:\stock_daily_app\outputs\x.csv",
        "token_configured": True,
    })
    assert value["stock_code"] == "000001"
    assert "api_key" not in value
    assert "confirmation_token" not in value
    assert "db_path" not in value
    assert "D:" not in value["message"]
    assert value["token_configured"] is True


def test_table_payload_is_plain_json_shape() -> None:
    payload = table_payload([{"code": "000001", "score": 0.2}, {"code": "600519", "score": 0.3}])
    assert payload["total"] == 2
    assert payload["records"][0]["code"] == "000001"
    assert {item["key"] for item in payload["columns"]} == {"code", "score"}


def test_ranking_page_exposes_model_score_alias_without_recalculation() -> None:
    service = WebReadApplicationService()
    service.ranking = lambda: pd.DataFrame([
        {"rank": 1, "code": "000001", "raw_score": 0.1234, "score": 0.9},
        {"rank": 2, "code": "600519", "pred_5d_ret": 0.0567, "score": 0.8},
    ])
    payload = service.ranking_page(offset=0, limit=10)
    records = payload["records"].to_dict("records")
    assert records[0]["pred_score"] == 0.1234
    # The browser alias is copied from the model output; the combined score remains unchanged.
    assert records[0]["score"] == 0.9
    assert records[1]["pred_score"] == 0.0567


def test_ranking_page_exposes_separate_top15_daily_statistics() -> None:
    service = WebReadApplicationService()
    service.ranking = lambda: pd.DataFrame([
        {
            "rank": 1,
            "code": "000001",
            "top5_daily_average_up_rate": 0.7,
            "top10_daily_average_up_rate": 0.65,
            "top15_daily_average_up_rate": 0.6,
            "top15_observation_days": 10,
            "top15_complete_days": 10,
            "top15_observation_count": 150,
            "top15_rise_count": 90,
            "top15_start_date": "2026-01-01",
            "top15_end_date": "2026-01-15",
            "calibration_top_k": 15,
            "calibration_target": "future_1d_ret_gt_0",
        }
    ])

    payload = service.ranking_page(offset=0, limit=10)
    statistics = payload["top15_statistics"]
    assert statistics == {
        "top5_daily_average_up_rate": 0.7,
        "top10_daily_average_up_rate": 0.65,
        "daily_average_up_rate": 0.6,
        "observation_days": 10,
        "complete_days": 10,
        "observation_count": 150,
        "rise_count": 90,
        "top_k": 15,
        "start_date": "2026-01-01",
        "end_date": "2026-01-15",
        "target": "future_1d_ret_gt_0",
    }
    assert present_ranking_page(payload)["top15_statistics"] == statistics


def test_ranking_page_exposes_target_mode_validation_against_universe() -> None:
    validation = {
        "valid_test_days": 20,
        "universe_next_day_up_probability": 0.5251,
        "top5_next_day_up_probability": 0.58,
        "top10_next_day_up_probability": 0.57,
        "top15_next_day_up_probability": 0.5467,
        "top5_lift_vs_universe": 0.0549,
        "top10_lift_vs_universe": 0.0449,
        "top15_lift_vs_universe": 0.0216,
        "all_topk_above_universe": True,
    }
    payload = {
        "records": [],
        "total": 0,
        "offset": 0,
        "limit": 100,
        "top15_statistics": None,
        "target_validation": validation,
    }
    assert present_ranking_page(payload)["target_validation"] == validation


def test_ranking_page_joins_signal_date_ohlc() -> None:
    service = WebReadApplicationService()
    service.ranking = lambda: pd.DataFrame([
        {"rank": 1, "code": "000001", "date": "2026-07-24", "raw_score": 0.1},
        {"rank": 2, "code": "600519.SH", "date": "20260724", "raw_score": 0.2},
    ])
    service.load_signal_ohlc_data = lambda: pd.DataFrame([
        {"code": "000001.SZ", "date": "20260724", "open": 10.1, "high": 10.8, "low": 9.9, "close": 10.5},
        {"code": "600519", "date": "2026-07-24", "open": 1500.0, "high": 1520.0, "low": 1490.0, "close": 1512.0},
    ])

    records = service.ranking_page(offset=0, limit=10)["records"].to_dict("records")
    assert records[0]["open"] == 10.1
    assert records[0]["close"] == 10.5
    assert bool(records[0]["ohlc_available"]) is True
    assert records[1]["high"] == 1520.0
    assert records[1]["low"] == 1490.0


def test_ranking_page_coerces_registered_scores_and_preserves_panel_close() -> None:
    service = WebReadApplicationService()
    service.ranking = lambda: pd.DataFrame([
        {
            "rank": "1",
            "code": "300759",
            "date": "2026-08-28",
            "model_score": "0.99542964",
            "member_1_rank_pct": "0.9908592",
            "member_2_rank_pct": "1.0",
            "historical_rank_bucket_up_rate": "0.5652173758",
            "close": "43.7800026",
        }
    ])
    service.load_signal_ohlc_data = lambda: pd.DataFrame()

    record = service.ranking_page(offset=0, limit=10)["records"].iloc[0]
    assert record["model_score"] == 0.99542964
    assert record["member_1_rank_pct"] == 0.9908592
    assert record["member_2_rank_pct"] == 1.0
    assert record["historical_rank_bucket_up_rate"] == 0.5652173758
    assert record["close"] == 43.7800026
    assert record["close_source"] == "model_panel_close"
    assert bool(record["ohlc_available"]) is False
