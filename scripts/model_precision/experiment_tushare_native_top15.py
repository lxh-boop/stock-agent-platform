from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.model_precision.experiment_master_top15 import DISCLAIMER
from scripts.model_precision.experiment_qlib_lightgbm_top15 import (
    CANDIDATES,
    fit_candidate,
)
from stock_ranker.tushare_panel import build_tushare_panel


DEFAULT_FEATURE_DIR = ROOT / "data" / "model_precision" / "tushare"
DEFAULT_REPORT = ROOT / "outputs" / "model_precision" / "tushare_native_top15.json"
DEFAULT_PREDICTIONS = ROOT / "data" / "model_precision" / "tushare_native_top15.parquet"
DEFAULT_MODEL_DIR = ROOT / "models" / "cross_sectional_lightgbm" / "experiments"


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pure-Tushare cross-sectional daily Top15 experiment without Kronos."
    )
    parser.add_argument("--feature-dir", type=Path, default=DEFAULT_FEATURE_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--predictions-path", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--end-date", default="2026-07-30")
    parser.add_argument("--iterations", type=int, default=160)
    parser.add_argument("--seed", type=int, default=101)
    parser.add_argument("--target-precision", type=float, default=0.55)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print("building pure-Tushare panel from existing local caches", flush=True)
    frame, features, data_audit = build_tushare_panel(
        args.feature_dir, end_date=args.end_date
    )
    print(f"panel={data_audit}", flush=True)

    validation_audits: list[dict[str, Any]] = []
    validation_scores = {}
    for index, candidate in enumerate(CANDIDATES, start=1):
        print(f"[{index}/{len(CANDIDATES)}] {candidate.name}", flush=True)
        _, scored, audit = fit_candidate(
            frame,
            features,
            candidate,
            score_year=2025,
            iterations=args.iterations,
            seed=args.seed,
        )
        validation_audits.append(audit)
        validation_scores[candidate.name] = scored
        print(f"2025 precision={audit['metrics']['precision']:.6f}", flush=True)
    best_audit = max(validation_audits, key=lambda item: item["metrics"]["precision"])
    best = next(
        candidate
        for candidate in CANDIDATES
        if candidate.name == best_audit["candidate"]["name"]
    )
    print(f"selected={best.name}; one 2026 holdout fit", flush=True)
    holdout_model, holdout_scores, holdout_audit = fit_candidate(
        frame,
        features,
        best,
        score_year=2026,
        iterations=args.iterations,
        seed=args.seed,
    )

    predictions = pd.concat(
        [validation_scores[best.name], holdout_scores], ignore_index=True
    )
    args.predictions_path.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_parquet(args.predictions_path, index=False)
    args.model_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.model_dir / f"tushare_native_{best.name}_through_2025.txt"
    holdout_model.save_model(checkpoint)
    validation = best_audit["metrics"]
    holdout = holdout_audit["metrics"]
    accepted = bool(
        validation["all_days_have_15"]
        and holdout["all_days_have_15"]
        and validation["precision"] >= args.target_precision
        and holdout["precision"] >= args.target_precision
    )
    importance = sorted(
        zip(features, holdout_model.feature_importance(importance_type="gain")),
        key=lambda item: item[1],
        reverse=True,
    )[:40]
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accepted": accepted,
        "objective": "pure_tushare_fixed_daily_top15_next_day_up_precision",
        "target_precision": float(args.target_precision),
        "selection_count_per_day": 15,
        "data_contract": {
            "source": str(args.feature_dir),
            "universe": "all unique date/code rows in local Tushare daily_basic cache",
            "label": "next available per-stock Tushare close > current close",
            "features": "close history + daily basic + money flow + margin + market indices",
            "kronos_model_used": False,
            "kronos_prediction_file_used": False,
            "kronos_market_history_used": False,
            "download_performed": False,
            "uses_future_features": False,
            "audit": data_audit,
        },
        "model_search": {
            "selection_period": "2025",
            "candidates": validation_audits,
            "selected": asdict(best),
            "iterations": args.iterations,
            "seed": args.seed,
        },
        "validation_2025": validation,
        "holdout_2026": holdout,
        "feature_count": len(features),
        "features": features,
        "top_feature_gain": [
            {"feature": feature, "gain": float(gain)} for feature, gain in importance
        ],
        "checkpoint": str(checkpoint),
        "checkpoint_research_only": True,
        "promotion_decision": "not_evaluated" if accepted else "rejected",
        "disclaimer": DISCLAIMER,
    }
    _atomic_json(report, args.report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
