from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.model_precision.experiment_master_top15 import (
    DISCLAIMER,
    FORBIDDEN_FEATURE_TERMS,
    load_tushare_labels,
    select_feature_columns,
)


DEFAULT_FEATURE_CACHE = (
    ROOT / "data" / "model_precision" / "stock_direction_features_v5_alpha.parquet"
)
DEFAULT_DAILY_BASIC = ROOT / "data" / "model_precision" / "tushare" / "daily_basic.csv"
DEFAULT_REPORT = ROOT / "outputs" / "model_precision" / "qlib_lightgbm_no_kronos_top15.json"
DEFAULT_PREDICTIONS = (
    ROOT / "data" / "model_precision" / "qlib_lightgbm_no_kronos_top15.parquet"
)
DEFAULT_MODEL_DIR = ROOT / "models" / "cross_sectional_lightgbm" / "experiments"


@dataclass(frozen=True)
class Candidate:
    name: str
    objective: str
    training_years: int = 0
    half_life_years: float = 0.0
    relevance: str = "up"


CANDIDATES = (
    Candidate("regression_expanding", "regression"),
    Candidate("binary_expanding", "binary"),
    Candidate("lambdarank_up_expanding", "lambdarank"),
    Candidate("rank_xendcg_up_expanding", "rank_xendcg"),
    Candidate("lambdarank_return5_expanding", "lambdarank", relevance="return5"),
    Candidate("lambdarank_up_3y", "lambdarank", training_years=3),
    Candidate("binary_3y", "binary", training_years=3),
    Candidate("lambdarank_up_decay3", "lambdarank", half_life_years=3.0),
)


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _metrics(frame: pd.DataFrame) -> dict[str, Any]:
    top = (
        frame.sort_values(
            ["date", "score", "code"],
            ascending=[True, False, True],
            kind="stable",
        )
        .groupby("date", sort=False)
        .head(15)
    )
    counts = top.groupby("date", sort=True).size()
    daily = top.groupby("date", sort=True)["label_up_tushare"].mean()
    monthly = (
        top.assign(month=top["date"].dt.to_period("M").astype(str))
        .groupby("month", sort=True)["label_up_tushare"]
        .agg(signals="size", correct="sum", precision="mean")
    )
    return {
        "start_date": str(daily.index.min().date()),
        "end_date": str(daily.index.max().date()),
        "days": int(len(daily)),
        "signals": int(len(top)),
        "correct": int(top["label_up_tushare"].sum()),
        "precision": float(daily.mean()),
        "all_days_have_15": bool(not counts.empty and counts.eq(15).all()),
        "monthly": [
            {
                "month": str(month),
                "signals": int(row["signals"]),
                "correct": int(row["correct"]),
                "precision": float(row["precision"]),
            }
            for month, row in monthly.iterrows()
        ],
    }


def _parameters(candidate: Candidate, seed: int) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "objective": candidate.objective,
        "learning_rate": 0.025,
        "num_leaves": 15,
        "max_depth": 4,
        "min_child_samples": 80,
        "feature_fraction": 0.90,
        "bagging_fraction": 0.85,
        "bagging_freq": 1,
        "lambda_l1": 0.30,
        "lambda_l2": 2.0,
        "seed": seed,
        "feature_fraction_seed": seed,
        "bagging_seed": seed,
        "data_random_seed": seed,
        "feature_pre_filter": False,
        "verbosity": -1,
        "num_threads": -1,
    }
    if candidate.objective in {"lambdarank", "rank_xendcg"}:
        parameters.update(
            metric="ndcg",
            ndcg_eval_at=[15],
            lambdarank_truncation_level=50,
            label_gain=[0, 1] if candidate.relevance == "up" else [0, 1, 3, 7, 15],
        )
    elif candidate.objective == "binary":
        parameters["metric"] = "binary_logloss"
    else:
        parameters["metric"] = "l2"
    return parameters


def _training_start(score_year: int, training_years: int) -> pd.Timestamp:
    if not training_years:
        return pd.Timestamp("2018-01-01")
    return pd.Timestamp(year=score_year - training_years, month=1, day=1)


def _return_relevance(frame: pd.DataFrame) -> np.ndarray:
    percentiles = frame.groupby("date", sort=False)["future_1d_ret_tushare"].rank(
        method="average", pct=True
    )
    return np.minimum((percentiles.to_numpy() * 5).astype("int8"), 4)


def _sample_weight(frame: pd.DataFrame, end: pd.Timestamp, half_life: float) -> np.ndarray | None:
    if half_life <= 0:
        return None
    age_years = (end - frame["date"]).dt.days.to_numpy(dtype="float64") / 365.25
    return np.power(0.5, age_years / half_life).astype("float32")


def fit_candidate(
    frame: pd.DataFrame,
    features: list[str],
    candidate: Candidate,
    *,
    score_year: int,
    iterations: int,
    seed: int,
) -> tuple[lgb.Booster, pd.DataFrame, dict[str, Any]]:
    train_end = pd.Timestamp(year=score_year - 1, month=12, day=31)
    train_start = _training_start(score_year, candidate.training_years)
    training = frame[
        frame["date"].between(train_start, train_end)
        & frame["label_up_tushare"].notna()
        & frame["future_1d_ret_tushare"].notna()
    ].copy()
    evaluation = frame[
        frame["date"].between(
            pd.Timestamp(year=score_year, month=1, day=1),
            pd.Timestamp(year=score_year, month=12, day=31),
        )
        & frame["label_up_tushare"].notna()
    ].copy()
    if training.empty or evaluation.empty:
        raise RuntimeError(f"empty training/evaluation split for {candidate.name}/{score_year}")

    if candidate.objective == "regression":
        label = training["future_1d_ret_tushare"].clip(-0.20, 0.20).to_numpy()
    elif candidate.relevance == "return5":
        label = _return_relevance(training)
    else:
        label = training["label_up_tushare"].astype("int8").to_numpy()

    dataset_kwargs: dict[str, Any] = {
        "data": training[features],
        "label": label,
        "weight": _sample_weight(training, train_end, candidate.half_life_years),
        "params": {"data_random_seed": seed, "feature_pre_filter": False},
        "free_raw_data": True,
    }
    if candidate.objective in {"lambdarank", "rank_xendcg"}:
        dataset_kwargs["group"] = training.groupby("date", sort=False).size().to_numpy()
    dataset = lgb.Dataset(**dataset_kwargs)
    model = lgb.train(
        _parameters(candidate, seed),
        dataset,
        num_boost_round=iterations,
        callbacks=[lgb.log_evaluation(0)],
    )
    evaluation["score"] = model.predict(evaluation[features]).astype("float32")
    scored = evaluation.loc[
        :, ["date", "code", "future_1d_ret_tushare", "label_up_tushare", "score"]
    ]
    audit = {
        "candidate": asdict(candidate),
        "score_year": score_year,
        "train_start": str(training["date"].min().date()),
        "train_end": str(training["date"].max().date()),
        "train_days": int(training["date"].nunique()),
        "train_rows": int(len(training)),
        "metrics": _metrics(scored),
    }
    return model, scored, audit


def _load_frame(args: argparse.Namespace) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
    schema = pq.ParquetFile(args.feature_cache).schema.names
    stock, market = select_feature_columns(schema)
    requested = ["date", "code", "label_up", *stock, *market]
    frame = pd.read_parquet(args.feature_cache, columns=requested)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    labels = load_tushare_labels(args.daily_basic)
    frame = frame.merge(labels, on=["date", "code"], how="left", validate="one_to_one")
    frame = frame.sort_values(["date", "code"], kind="stable").reset_index(drop=True)
    frame.loc[:, [*stock, *market]] = frame.loc[:, [*stock, *market]].replace(
        [np.inf, -np.inf], np.nan
    )

    coverage_training = frame[frame["date"].le("2024-12-31")]
    selected = [
        column
        for column in [*stock, *market]
        if coverage_training[column].notna().mean() >= 0.80
        and coverage_training[column].std(skipna=True) > 1e-8
    ]
    compared = frame.dropna(subset=["label_up", "label_up_tushare"])
    audit = {
        "rows": int(len(frame)),
        "stocks": int(frame["code"].nunique()),
        "days": int(frame["date"].nunique()),
        "feature_count": len(selected),
        "stock_feature_count": sum(column in stock for column in selected),
        "market_feature_count": sum(column in market for column in selected),
        "feature_coverage_rule": ">=80% non-null through 2024-12-31",
        "tushare_vs_legacy_label_agreement": float(
            compared["label_up_tushare"].eq(compared["label_up"]).mean()
        ),
    }
    return frame, selected, audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Qlib-style LightGBM cross-sectional Top15 experiment without Kronos."
    )
    parser.add_argument("--feature-cache", type=Path, default=DEFAULT_FEATURE_CACHE)
    parser.add_argument("--daily-basic", type=Path, default=DEFAULT_DAILY_BASIC)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--predictions-path", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--iterations", type=int, default=160)
    parser.add_argument("--seed", type=int, default=101)
    parser.add_argument("--target-precision", type=float, default=0.55)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    frame, features, data_audit = _load_frame(args)
    forbidden = [
        feature
        for feature in features
        if any(term in feature.lower() for term in FORBIDDEN_FEATURE_TERMS)
    ]
    if forbidden:
        raise AssertionError(f"forbidden Kronos-lineage features selected: {forbidden}")
    print(
        f"loaded rows={len(frame)} features={len(features)}; validation candidate search",
        flush=True,
    )

    validation_audits: list[dict[str, Any]] = []
    validation_scores: dict[str, pd.DataFrame] = {}
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
        print(
            f"{candidate.name} 2025 precision={audit['metrics']['precision']:.6f}",
            flush=True,
        )

    best_audit = max(validation_audits, key=lambda item: item["metrics"]["precision"])
    best = next(
        candidate
        for candidate in CANDIDATES
        if candidate.name == best_audit["candidate"]["name"]
    )
    print(f"selected on 2025: {best.name}; fitting once for 2026 holdout", flush=True)
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
    checkpoint = args.model_dir / f"{best.name}_through_2025.txt"
    holdout_model.save_model(checkpoint)

    validation_metrics = best_audit["metrics"]
    holdout_metrics = holdout_audit["metrics"]
    accepted = bool(
        validation_metrics["all_days_have_15"]
        and holdout_metrics["all_days_have_15"]
        and validation_metrics["precision"] >= args.target_precision
        and holdout_metrics["precision"] >= args.target_precision
    )
    importance = sorted(
        zip(features, holdout_model.feature_importance(importance_type="gain")),
        key=lambda item: item[1],
        reverse=True,
    )[:30]
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accepted": accepted,
        "objective": "no_kronos_qlib_style_lightgbm_fixed_daily_top15_next_day_up_precision",
        "target_precision": float(args.target_precision),
        "selection_count_per_day": 15,
        "model_search": {
            "selection_period": "2025",
            "candidate_count": len(CANDIDATES),
            "candidates": validation_audits,
            "selected": asdict(best),
            "iterations": args.iterations,
            "seed": args.seed,
        },
        "validation_2025": validation_metrics,
        "holdout_2026": holdout_metrics,
        "data_contract": {
            "feature_source": "Tushare-derived cached fundamentals, money flow, margin and index state",
            "label_source": str(args.daily_basic),
            "label": "exact next available trading-day Tushare close > current close",
            "kronos_model_used": False,
            "kronos_predictions_used_as_features": False,
            "legacy_prediction_artifact_used_only_as_historical_universe_keys": True,
            "uses_future_features": False,
            "forbidden_kronos_lineage_features": forbidden,
            "audit": data_audit,
        },
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
