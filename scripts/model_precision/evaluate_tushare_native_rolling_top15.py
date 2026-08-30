from __future__ import annotations

import argparse
import gc
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.model_precision.experiment_master_top15 import DISCLAIMER
from scripts.model_precision.experiment_qlib_lightgbm_top15 import _metrics
from scripts.model_precision.search_tushare_native_rankers import (
    CONFIGS,
    _blend,
    _features,
    _parameters,
)
from stock_ranker.tushare_panel import build_tushare_panel


MEMBERS = ("lambda_all_t15", "lambda_technical")
DEFAULT_FEATURE_DIR = ROOT / "data" / "model_precision" / "tushare"
DEFAULT_REPORT = ROOT / "outputs" / "model_precision" / "tushare_native_rolling_top15.json"
DEFAULT_PREDICTIONS = (
    ROOT / "data" / "model_precision" / "tushare_native_rolling_top15.parquet"
)


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _fit_period(
    frame: pd.DataFrame,
    all_features: list[str],
    member: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    config = next(value for value in CONFIGS if value.name == member)
    columns = _features(all_features, config.subset)
    training = frame[
        frame["date"].lt(start) & frame["label_up_tushare"].notna()
    ]
    evaluation = frame[
        frame["date"].between(start, end) & frame["label_up_tushare"].notna()
    ].copy()
    dataset = lgb.Dataset(
        training[columns],
        label=training["label_up_tushare"].astype("int8"),
        group=training.groupby("date", sort=False).size().to_numpy(),
        params={"data_random_seed": config.seed, "feature_pre_filter": False},
        free_raw_data=True,
    )
    model = lgb.train(
        _parameters(config),
        dataset,
        num_boost_round=config.iterations,
        callbacks=[lgb.log_evaluation(0)],
    )
    evaluation["score"] = model.predict(evaluation[columns]).astype("float32")
    result = evaluation.loc[
        :, ["date", "code", "future_1d_ret_tushare", "label_up_tushare", "score"]
    ]
    del model, dataset
    gc.collect()
    return result


def _periods() -> list[tuple[str, str, str]]:
    return [
        ("2025Q1", "2025-01-01", "2025-03-31"),
        ("2025Q2", "2025-04-01", "2025-06-30"),
        ("2025Q3", "2025-07-01", "2025-09-30"),
        ("2025Q4", "2025-10-01", "2025-12-31"),
        ("2026Q1", "2026-01-01", "2026-03-31"),
        ("2026Q2", "2026-04-01", "2026-06-30"),
        ("2026Q3", "2026-07-01", "2026-07-30"),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Quarterly expanding walk-forward for the selected pure-Tushare ensemble."
    )
    parser.add_argument("--feature-dir", type=Path, default=DEFAULT_FEATURE_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--predictions-path", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--target-precision", type=float, default=0.55)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    frame, features, data_audit = build_tushare_panel(args.feature_dir)
    parts: list[pd.DataFrame] = []
    period_reports: list[dict[str, Any]] = []
    for index, (name, start, end) in enumerate(_periods(), start=1):
        print(f"[{index}/{len(_periods())}] {name} train before {start}", flush=True)
        member_scores = {
            member: _fit_period(frame, features, member, start, end)
            for member in MEMBERS
        }
        blended = _blend(member_scores, MEMBERS)
        blended["period"] = name
        parts.append(blended)
        metrics = _metrics(blended)
        period_reports.append({"period": name, "train_end_exclusive": start, **metrics})
        print(f"{name} precision={metrics['precision']:.6f}", flush=True)
    predictions = pd.concat(parts, ignore_index=True)
    metrics_2025 = _metrics(predictions[predictions["date"].dt.year.eq(2025)])
    metrics_2026 = _metrics(predictions[predictions["date"].dt.year.eq(2026)])
    accepted = bool(
        metrics_2025["all_days_have_15"]
        and metrics_2026["all_days_have_15"]
        and metrics_2025["precision"] >= args.target_precision
        and metrics_2026["precision"] >= args.target_precision
    )
    args.predictions_path.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_parquet(args.predictions_path, index=False)
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accepted": accepted,
        "objective": "quarterly_expanding_pure_tushare_fixed_daily_top15",
        "target_precision": float(args.target_precision),
        "selection_count_per_day": 15,
        "members_fixed_before_rolling_test": list(MEMBERS),
        "rolling_schedule": "calendar quarter; fit only rows earlier than the scoring period",
        "data_contract": {
            "kronos_model_used": False,
            "kronos_prediction_file_used": False,
            "kronos_market_history_used": False,
            "download_performed": False,
            "uses_future_features": False,
            "audit": data_audit,
        },
        "periods": period_reports,
        "validation_2025": metrics_2025,
        "holdout_2026": metrics_2026,
        "promotion_decision": "not_evaluated" if accepted else "rejected",
        "disclaimer": DISCLAIMER,
    }
    _atomic_json(report, args.report_path)
    print(
        json.dumps(
            {
                "accepted": accepted,
                "validation_2025": metrics_2025,
                "holdout_2026": metrics_2026,
                "periods": period_reports,
                "report": str(args.report_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
