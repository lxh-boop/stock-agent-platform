from __future__ import annotations

import argparse
import copy
import gc
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.model_precision.experiment_master_top15 import (
    DISCLAIMER,
    DailySequencePanel,
    ExperimentConfig,
    predict,
    robust_normalize,
    top15_metrics,
    train_model,
)
from stock_ranker.tushare_panel import build_tushare_panel


DEFAULT_FEATURE_DIR = ROOT / "data" / "model_precision" / "tushare"
DEFAULT_REPORT = ROOT / "outputs" / "model_precision" / "master_tushare_native_top15.json"
DEFAULT_PREDICTIONS = ROOT / "data" / "model_precision" / "master_tushare_native_top15.parquet"
DEFAULT_MODEL_DIR = ROOT / "models" / "cross_sectional_master" / "experiments"
LOSSES = ("bce", "pairwise", "rank_bce")


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _panel(
    frame: pd.DataFrame,
    ordered_features: list[str],
    training_end: str,
    sequence_length: int,
) -> tuple[DailySequencePanel, dict[str, Any]]:
    raw = frame.loc[:, ordered_features].to_numpy(dtype="float32")
    mask = frame["date"].le(training_end).to_numpy()
    normalized, state = robust_normalize(raw, mask)
    del raw
    gc.collect()
    return DailySequencePanel(
        frame.loc[:, ["date", "code", "future_1d_ret_tushare", "label_up_tushare"]],
        normalized,
        sequence_length=sequence_length,
    ), {
        "training_end": training_end,
        "normalization_median_finite": bool(np.isfinite(state["median"]).all()),
        "normalization_scale_positive": bool((state["scale"] > 0).all()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MASTER on the complete local pure-Tushare cross-section."
    )
    parser.add_argument("--feature-dir", type=Path, default=DEFAULT_FEATURE_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--predictions-path", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--sequence-length", type=int, default=8)
    parser.add_argument("--model-dimension", type=int, default=32)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--target-precision", type=float, default=0.55)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    torch.set_num_threads(max(1, min(8, os.cpu_count() or 1)))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    frame, features, data_audit = build_tushare_panel(args.feature_dir)
    market = [column for column in features if column.startswith("market_")]
    stock = [column for column in features if column not in market]
    ordered = [*stock, *market]
    base = ExperimentConfig(
        sequence_length=args.sequence_length,
        model_dimension=args.model_dimension,
        temporal_heads=4,
        stock_heads=4,
        dropout=0.1,
        gate_temperature=5.0,
        learning_rate=1e-4,
        weight_decay=1e-5,
        epochs=args.epochs,
        seed=args.seed,
        max_train_stocks=256,
        loss="bce",
    )

    print(f"device={device} features={len(features)} building 2025 panel", flush=True)
    panel_2025, normalize_2025 = _panel(
        frame, ordered, "2024-12-31", args.sequence_length
    )
    train_dates = panel_2025.dates_between("2018-03-01", "2024-12-31")
    validation_dates = panel_2025.dates_between("2025-01-01", "2025-12-31")
    validation_reports: list[dict[str, Any]] = []
    validation_scores: dict[str, pd.DataFrame] = {}
    for loss in LOSSES:
        print(f"validation loss={loss}", flush=True)
        config = replace(base, loss=loss)
        model, history = train_model(
            panel_2025,
            dates=train_dates,
            stock_feature_count=len(stock),
            market_feature_count=len(market),
            config=config,
            device=device,
        )
        scored = predict(model, panel_2025, validation_dates, device)
        metrics = top15_metrics(scored)
        validation_scores[loss] = scored
        validation_reports.append(
            {"loss": loss, "loss_history": history, "metrics": metrics}
        )
        print(f"loss={loss} precision={metrics['precision']:.6f}", flush=True)
        del model
        gc.collect()
    selected = max(validation_reports, key=lambda item: item["metrics"]["precision"])
    selected_loss = str(selected["loss"])
    print(f"selected={selected_loss}; building 2026 panel", flush=True)
    del panel_2025
    gc.collect()

    panel_2026, normalize_2026 = _panel(
        frame, ordered, "2025-12-31", args.sequence_length
    )
    del frame
    gc.collect()
    config = replace(base, loss=selected_loss)
    model, holdout_history = train_model(
        panel_2026,
        dates=panel_2026.dates_between("2018-03-01", "2025-12-31"),
        stock_feature_count=len(stock),
        market_feature_count=len(market),
        config=config,
        device=device,
    )
    holdout_scores = predict(
        model,
        panel_2026,
        panel_2026.dates_between("2026-01-01", "2026-07-30"),
        device,
    )
    validation_metrics = selected["metrics"]
    holdout_metrics = top15_metrics(holdout_scores)
    predictions = pd.concat(
        [validation_scores[selected_loss], holdout_scores], ignore_index=True
    )
    args.predictions_path.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_parquet(args.predictions_path, index=False)
    args.model_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.model_dir / f"master_tushare_native_{selected_loss}_through_2025.pt"
    torch.save(
        {
            "state_dict": copy.deepcopy(model.state_dict()),
            "stock_features": stock,
            "market_features": market,
            "config": asdict(config),
            "trained_through": "2025-12-31",
            "research_only": True,
        },
        checkpoint,
    )
    accepted = bool(
        validation_metrics["all_days_have_15"]
        and holdout_metrics["all_days_have_15"]
        and validation_metrics["precision"] >= args.target_precision
        and holdout_metrics["precision"] >= args.target_precision
    )
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accepted": accepted,
        "objective": "master_pure_tushare_fixed_daily_top15",
        "target_precision": float(args.target_precision),
        "selection_count_per_day": 15,
        "model": {
            "name": "MASTER",
            "architecture": "market gate + temporal attention + cross-stock attention",
            "config": asdict(config),
            "checkpoint": str(checkpoint),
            "research_only": True,
        },
        "data_contract": {
            "kronos_model_used": False,
            "kronos_prediction_file_used": False,
            "kronos_market_history_used": False,
            "download_performed": False,
            "uses_future_features": False,
            "audit": data_audit,
            "normalize_2025": normalize_2025,
            "normalize_2026": normalize_2026,
        },
        "validation_loss_search": validation_reports,
        "selected_loss": selected_loss,
        "validation_2025": validation_metrics,
        "holdout_2026": holdout_metrics,
        "holdout_loss_history": holdout_history,
        "promotion_decision": "not_evaluated" if accepted else "rejected",
        "disclaimer": DISCLAIMER,
    }
    _atomic_json(report, args.report_path)
    print(
        json.dumps(
            {
                "accepted": accepted,
                "selected_loss": selected_loss,
                "validation_2025": validation_metrics,
                "holdout_2026": holdout_metrics,
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
