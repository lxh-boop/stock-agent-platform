from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_metrics import summarize_ic
from config import LATEST_RAW_DATA_PATH
from factor_provider import build_alpha158
from model_zoo.metadata import get_model_metadata
from model_zoo.registry import get_model_entry
from model_zoo_backend import load_zoo_adapter


COMPARE_PATH = Path("outputs") / "external_model_compare.csv"
ERRORS_PATH = Path("outputs") / "external_model_errors.json"
PREDICTION_DIR = Path("outputs") / "external_model_predictions"


def _load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    raw_path = Path(LATEST_RAW_DATA_PATH)
    if not raw_path.exists():
        raise FileNotFoundError(f"missing raw data cache: {raw_path}")

    raw = pd.read_csv(raw_path, dtype={"code": str}, encoding="utf-8-sig")
    raw["date"] = pd.to_datetime(raw["date"])
    raw["code"] = raw["code"].astype(str).str.zfill(6)
    feature = build_alpha158(raw, output_trading_days=120)
    return raw, feature


def _prediction_dates(
    feature_data: pd.DataFrame,
    backtest_days: int,
    start_date=None,
    end_date=None,
) -> list[pd.Timestamp]:
    data = feature_data.dropna(subset=["future_5d_ret"]).copy()
    dates = pd.Series(pd.to_datetime(data["date"].unique())).sort_values()
    if start_date:
        dates = dates[dates >= pd.to_datetime(start_date)]
    if end_date:
        dates = dates[dates <= pd.to_datetime(end_date)]
    return list(dates.tail(int(backtest_days)))


def evaluate_model(
    model_name: str,
    raw_data: pd.DataFrame,
    feature_data: pd.DataFrame,
    prediction_dates: list[pd.Timestamp],
    device: str = "cpu",
    context_length: int = 64,
    batch_size: int = 64,
) -> tuple[pd.DataFrame, dict]:
    entry = get_model_entry(model_name)
    meta = get_model_metadata(entry.name) or {}
    if meta.get("status") != "downloaded":
        raise RuntimeError(
            f"{entry.name} has not been downloaded. "
            f"Run: python -m model_zoo.downloader --model {entry.name}"
        )

    start = time.perf_counter()
    adapter = load_zoo_adapter(
        model_name=entry.name,
        device=device,
        context_length=context_length,
        batch_size=batch_size,
    )
    pred = adapter.predict_windows(
        raw_data=raw_data,
        feature_data=feature_data,
        prediction_dates=prediction_dates,
        min_context=min(32, context_length),
    )
    elapsed = time.perf_counter() - start

    pred = pred.dropna(subset=["future_5d_ret"]).copy()
    if pred.empty:
        raise RuntimeError(f"{entry.name} produced no labeled historical predictions.")

    PREDICTION_DIR.mkdir(parents=True, exist_ok=True)
    pred_path = PREDICTION_DIR / f"{entry.name}_predictions.csv"
    pred.to_csv(pred_path, index=False, encoding="utf-8-sig")

    ic_metrics = summarize_ic(pred)
    up_true = (pd.to_numeric(pred["future_5d_ret"], errors="coerce") > 0).astype(int)
    try:
        from sklearn.metrics import mean_squared_error, roc_auc_score

        auc = roc_auc_score(up_true, pd.to_numeric(pred["up_prob"], errors="coerce"))
        mse = mean_squared_error(pred["future_5d_ret"], pred["pred_5d_ret"])
    except Exception:
        auc = np.nan
        mse = np.nan

    sorted_pred = pred.sort_values(["date", "score"], ascending=[True, False])
    top10 = sorted_pred.groupby("date").head(10).groupby("date")["future_5d_ret"].mean().mean()
    top30 = sorted_pred.groupby("date").head(30).groupby("date")["future_5d_ret"].mean().mean()

    metrics = {
        "model_name": entry.name,
        "hf_repo": entry.hf_repo,
        "status": "success",
        "prediction_file": str(pred_path),
        "prediction_rows": int(len(pred)),
        "prediction_days": int(pd.to_datetime(pred["date"]).nunique()),
        "inference_seconds": float(elapsed),
        "Top10Ret": float(top10) if pd.notna(top10) else np.nan,
        "Top30Ret": float(top30) if pd.notna(top30) else np.nan,
        "AUC": float(auc) if pd.notna(auc) else np.nan,
        "MSE": float(mse) if pd.notna(mse) else np.nan,
        **ic_metrics,
    }
    return pred, metrics


def run_evaluation(args: argparse.Namespace) -> tuple[pd.DataFrame, dict]:
    raw, feature = _load_data()
    dates = _prediction_dates(feature, args.backtest_days, args.start_date, args.end_date)
    if not dates:
        raise RuntimeError("no historical dates available for evaluation")

    rows = []
    errors = {}
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        try:
            _, metrics = evaluate_model(
                model_name=model,
                raw_data=raw,
                feature_data=feature,
                prediction_dates=dates,
                device=args.device,
                context_length=args.context_length,
                batch_size=args.batch_size,
            )
            rows.append(metrics)
        except Exception as exc:
            entry_name = model
            try:
                entry_name = get_model_entry(model).name
            except Exception:
                pass
            errors[entry_name] = str(exc)
            rows.append({"model_name": entry_name, "status": "failed", "error": str(exc)})

    COMPARE_PATH.parent.mkdir(parents=True, exist_ok=True)
    compare = pd.DataFrame(rows)
    compare.to_csv(COMPARE_PATH, index=False, encoding="utf-8-sig")
    ERRORS_PATH.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    return compare, errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True)
    parser.add_argument("--universe", default="csi300")
    parser.add_argument("--backtest-days", type=int, default=60)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--context-length", type=int, default=64)
    parser.add_argument("--batch-size", type=int, default=64)
    return parser.parse_args()


if __name__ == "__main__":
    compare_df, errors = run_evaluation(parse_args())
    print(compare_df.to_string(index=False))
    if errors:
        print(json.dumps(errors, ensure_ascii=False, indent=2))
