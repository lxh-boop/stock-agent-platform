from __future__ import annotations

import pandas as pd

import backtest
from database.repositories.prediction_repository import PredictionRepository
from ranking_runtime.settings import ACTIVE_MODEL_BACKEND, ACTIVE_MODEL_NAME


def test_prediction_payload_normalizes_registered_ranker_types() -> None:
    normalized = PredictionRepository.normalize_ranking_record(
        {
            "date": "2026-08-28",
            "prediction_date": "2026-08-31",
            "code": "300759",
            "model_name": ACTIVE_MODEL_NAME,
            "rank": "1",
            "model_score": "0.99542964",
            "member_1_rank_pct": "0.9908592",
            "member_2_rank_pct": "1.0",
            "top15_up_signal": "True",
            "close": "43.7800026",
        }
    )
    payload = __import__("json").loads(normalized["payload_json"])
    assert payload["rank"] == 1
    assert payload["model_score"] == 0.99542964
    assert payload["member_1_rank_pct"] == 0.9908592
    assert payload["member_2_rank_pct"] == 1.0
    assert payload["top15_up_signal"] is True
    assert payload["close"] == 43.7800026


def test_default_backtest_routes_to_registered_ranker(monkeypatch) -> None:
    expected = (pd.DataFrame([{"nav": 1.0}]), {"model_name": ACTIVE_MODEL_NAME}, pd.DataFrame())
    seen: dict[str, object] = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return expected

    monkeypatch.setattr("ranking_runtime.backtest.run_registered_ranker_backtest", fake_run)
    actual = backtest.run_latest_t1_backtest()
    assert actual is expected
    assert seen["topk"] == 15
    assert backtest.DEFAULT_BACKTEST_BACKEND == ACTIVE_MODEL_BACKEND
