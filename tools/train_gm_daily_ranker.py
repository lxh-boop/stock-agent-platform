from __future__ import annotations

"""Train the single LightGBM model used by tools/gm_daily_refresh.py."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_gm import GM_DAILY_FEATURE_COLUMNS, build_gm_daily_feature_history


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", required=True, help="normalized GM daily bars CSV")
    parser.add_argument("--train-start", required=True)
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--validation-start", required=True)
    parser.add_argument("--validation-end", required=True)
    parser.add_argument("--model-output", default=str(ROOT / "models" / "gm_daily_ranker" / "model.txt"))
    parser.add_argument(
        "--metrics-output",
        default=str(ROOT / "outputs" / "gm" / "gm_daily_ranker_training_metrics.json"),
    )
    return parser.parse_args()


def _date_slice(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return frame[frame["date"].between(str(start), str(end))].copy()


def main() -> int:
    args = parse_args()
    if str(args.validation_start) <= str(args.train_end):
        raise ValueError("validation_start_must_be_after_train_end")

    bars = pd.read_csv(args.bars, dtype={"code": str, "symbol": str})
    features = build_gm_daily_feature_history(bars).sort_values(
        ["code", "date"], kind="stable"
    ).reset_index(drop=True)

    source = bars.copy()
    source["date"] = source["date"].astype(str)
    source["code"] = source["code"].astype(str).str.zfill(6)
    source["close"] = pd.to_numeric(source["close"], errors="coerce")
    source = source.sort_values(["code", "date"]).drop_duplicates(["code", "date"], keep="last")
    features = features.merge(
        source.loc[:, ["date", "code", "close"]].rename(columns={"close": "signal_close"}),
        on=["date", "code"],
        how="left",
        validate="one_to_one",
    )
    grouped = source.groupby("code", sort=False)
    source["next_close"] = grouped["close"].shift(-1)
    features = features.merge(
        source.loc[:, ["date", "code", "next_close"]],
        on=["date", "code"],
        how="left",
        validate="one_to_one",
    )
    # The final date in each code's history has no future close, so it must be
    # excluded rather than treated as a negative label.
    features["label_up"] = np.where(
        features["next_close"].notna(),
        features["next_close"].gt(features["signal_close"]).astype(np.float32),
        np.nan,
    )

    train = _date_slice(features, args.train_start, args.train_end)
    validation = _date_slice(features, args.validation_start, args.validation_end)
    train = train.dropna(subset=["label_up"])
    validation = validation.dropna(subset=["label_up"])
    if train.empty or validation.empty:
        raise RuntimeError("training_or_validation_empty")

    train_dataset = lgb.Dataset(
        train.loc[:, GM_DAILY_FEATURE_COLUMNS],
        label=train["label_up"],
        feature_name=GM_DAILY_FEATURE_COLUMNS,
        free_raw_data=False,
    )
    validation_dataset = lgb.Dataset(
        validation.loc[:, GM_DAILY_FEATURE_COLUMNS],
        label=validation["label_up"],
        reference=train_dataset,
        feature_name=GM_DAILY_FEATURE_COLUMNS,
        free_raw_data=False,
    )
    params = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_data_in_leaf": 50,
        "feature_fraction": 0.85,
        "bagging_fraction": 0.85,
        "bagging_freq": 1,
        "lambda_l2": 1.0,
        "verbosity": -1,
        "seed": 42,
    }
    booster = lgb.train(
        params,
        train_dataset,
        num_boost_round=600,
        valid_sets=[validation_dataset],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)],
    )

    model_path = Path(args.model_output)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(model_path))

    validation_score = booster.predict(validation.loc[:, GM_DAILY_FEATURE_COLUMNS])
    labels = validation["label_up"].astype(int).to_numpy()
    validation_auc = (
        float(roc_auc_score(labels, validation_score))
        if np.unique(labels).size == 2
        else None
    )
    metrics = {
        "trained_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "train_rows": int(len(train)),
        "train_start": args.train_start,
        "train_end": args.train_end,
        "validation_rows": int(len(validation)),
        "validation_start": args.validation_start,
        "validation_end": args.validation_end,
        "best_iteration": int(booster.best_iteration or 0),
        "validation_auc": validation_auc,
        "feature_count": len(GM_DAILY_FEATURE_COLUMNS),
        "feature_columns": GM_DAILY_FEATURE_COLUMNS,
        "model_path": str(model_path.resolve()),
        "parameters": params,
    }
    metrics_path = Path(args.metrics_output)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
