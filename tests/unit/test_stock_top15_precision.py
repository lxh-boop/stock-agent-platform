from __future__ import annotations

from pathlib import Path

import pandas as pd

from kronos_runtime.stock_direction_features import _merge_asof_events
from pipelines.prediction_pipeline import run_prediction_pipeline
from pipelines.schemas import PipelineContext, PipelineStatus
from ranking_runtime.settings import ACTIVE_MODEL_NAME


def _ranking(rows: int = 20) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "rank": index,
                "date": "2026-08-07",
                "prediction_date": "2026-08-10",
                "code": f"{index:06d}",
                "pred_score": 0.10 - index / 1000,
                "pred_return": 0.10 - index / 1000,
                "model_name": ACTIVE_MODEL_NAME,
                "top15_up_signal": index <= 15,
            }
            for index in range(1, rows + 1)
        ]
    )


def test_prediction_pipeline_always_emits_fixed_top15(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    output.mkdir()
    _ranking().to_csv(output / "ranking_latest.csv", index=False, encoding="utf-8-sig")

    result = run_prediction_pipeline(
        PipelineContext(output_dir=output, top_k=50),
        ranking_path=output / "ranking_latest.csv",
    )

    assert result.status == PipelineStatus.SUCCESS
    assert result.input_count == 20
    assert result.output_count == 15
    assert [item.stock_code for item in result.predictions] == [
        f"{index:06d}" for index in range(1, 16)
    ]


def test_prediction_pipeline_rejects_incomplete_fixed_top15(tmp_path: Path) -> None:
    output = tmp_path / "outputs"
    output.mkdir()
    ranking = _ranking()
    ranking.loc[ranking["rank"].eq(15), "top15_up_signal"] = False
    ranking.to_csv(output / "ranking_latest.csv", index=False, encoding="utf-8-sig")

    result = run_prediction_pipeline(
        PipelineContext(output_dir=output, top_k=50),
        ranking_path=output / "ranking_latest.csv",
    )

    assert result.status == PipelineStatus.FAILED
    assert result.output_count == 0
    assert "Top15" in result.message


def test_daily_top15_precision_is_mean_of_each_days_fifteen_labels() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2026-01-05"] * 15 + ["2026-01-06"] * 15,
            "label_up": [1] * 9 + [0] * 6 + [1] * 6 + [0] * 9,
        }
    )

    daily_average = frame.groupby("date")["label_up"].mean().mean()

    assert daily_average == 0.5
    assert daily_average == frame["label_up"].mean()


def test_event_features_are_only_visible_on_or_after_announcement_date() -> None:
    base = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-01-01", "2025-01-03"]),
            "code": pd.Series(["000001", "000001"], dtype=object),
            "existing": [1.0, 2.0],
        }
    )
    events = pd.DataFrame(
        {
            "code": pd.Series(["000001"], dtype="string"),
            "event_date": pd.to_datetime(["2025-01-02"]),
            "event_value": [7.0],
        }
    )

    merged = _merge_asof_events(
        base,
        events,
        feature_columns=["event_value"],
        prefix="event_test",
    )

    assert pd.isna(merged.loc[0, "event_value"])
    assert merged.loc[1, "event_value"] == 7.0
    assert merged.loc[1, "event_test_days_since"] == 1.0
