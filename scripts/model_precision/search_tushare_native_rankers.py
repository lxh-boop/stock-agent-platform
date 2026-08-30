from __future__ import annotations

import argparse
import gc
import itertools
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


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.model_precision.experiment_master_top15 import DISCLAIMER
from scripts.model_precision.experiment_qlib_lightgbm_top15 import _metrics
from stock_ranker.tushare_panel import build_tushare_panel


DEFAULT_FEATURE_DIR = ROOT / "data" / "model_precision" / "tushare"
DEFAULT_REPORT = ROOT / "outputs" / "model_precision" / "tushare_native_ranker_search.json"
DEFAULT_PREDICTIONS = (
    ROOT / "data" / "model_precision" / "tushare_native_ranker_search.parquet"
)
DEFAULT_MODEL_DIR = ROOT / "models" / "cross_sectional_lightgbm" / "experiments"


@dataclass(frozen=True)
class SearchConfig:
    name: str
    objective: str = "rank_xendcg"
    subset: str = "all"
    iterations: int = 160
    num_leaves: int = 15
    max_depth: int = 4
    min_child_samples: int = 80
    feature_fraction: float = 0.90
    truncation: int = 50
    seed: int = 101


CONFIGS = (
    SearchConfig("xendcg_all_base"),
    SearchConfig("lambda_all_base", objective="lambdarank"),
    SearchConfig("xendcg_all_round80", iterations=80),
    SearchConfig("xendcg_all_round240", iterations=240),
    SearchConfig(
        "xendcg_all_depth3", num_leaves=7, max_depth=3, min_child_samples=120,
        feature_fraction=0.80, truncation=15,
    ),
    SearchConfig(
        "xendcg_all_depth6", num_leaves=31, max_depth=6, min_child_samples=160,
        feature_fraction=0.70,
    ),
    SearchConfig("lambda_all_t15", objective="lambdarank", truncation=15),
    SearchConfig("lambda_all_t30", objective="lambdarank", truncation=30),
    SearchConfig("xendcg_no_market", subset="no_market"),
    SearchConfig("lambda_no_market", objective="lambdarank", subset="no_market"),
    SearchConfig("xendcg_technical", subset="technical"),
    SearchConfig("lambda_technical", objective="lambdarank", subset="technical"),
    SearchConfig("xendcg_rank_focused", subset="rank_focused"),
)


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _features(all_features: list[str], subset: str) -> list[str]:
    if subset == "all":
        return all_features
    if subset == "no_market":
        return [column for column in all_features if not column.startswith("market_")]
    if subset == "technical":
        return [
            column
            for column in all_features
            if column.startswith(("tech_", "cross_pct_tech_"))
            or column in {
                "basic_log_close",
                "cross_pct_basic_log_close",
                "is_shanghai",
                "is_chinext",
                "is_star",
                "month",
                "weekday",
            }
        ]
    if subset == "rank_focused":
        return [
            column
            for column in all_features
            if column.startswith("cross_pct_")
            or column.startswith("market_prior_up_rate_")
            or column in {"is_shanghai", "is_chinext", "is_star", "month", "weekday"}
        ]
    raise ValueError(f"unknown subset: {subset}")


def _parameters(config: SearchConfig) -> dict[str, Any]:
    return {
        "objective": config.objective,
        "metric": "ndcg",
        "ndcg_eval_at": [15],
        "lambdarank_truncation_level": config.truncation,
        "label_gain": [0, 1],
        "learning_rate": 0.025,
        "num_leaves": config.num_leaves,
        "max_depth": config.max_depth,
        "min_child_samples": config.min_child_samples,
        "feature_fraction": config.feature_fraction,
        "bagging_fraction": 0.85,
        "bagging_freq": 1,
        "lambda_l1": 0.30,
        "lambda_l2": 2.0,
        "seed": config.seed,
        "feature_fraction_seed": config.seed,
        "bagging_seed": config.seed,
        "data_random_seed": config.seed,
        "feature_pre_filter": False,
        "verbosity": -1,
        "num_threads": -1,
    }


def _fit(
    frame: pd.DataFrame,
    all_features: list[str],
    config: SearchConfig,
    score_year: int,
) -> tuple[lgb.Booster, pd.DataFrame, dict[str, Any]]:
    columns = _features(all_features, config.subset)
    training = frame[
        frame["date"].lt(f"{score_year}-01-01")
        & frame["label_up_tushare"].notna()
    ]
    evaluation = frame[
        frame["date"].between(f"{score_year}-01-01", f"{score_year}-12-31")
        & frame["label_up_tushare"].notna()
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
    scored = evaluation.loc[
        :, ["date", "code", "future_1d_ret_tushare", "label_up_tushare", "score"]
    ]
    return model, scored, {
        "config": asdict(config),
        "feature_count": len(columns),
        "score_year": score_year,
        "train_rows": int(len(training)),
        "train_days": int(training["date"].nunique()),
        "metrics": _metrics(scored),
    }


def _blend(scored: dict[str, pd.DataFrame], names: tuple[str, ...]) -> pd.DataFrame:
    base = scored[names[0]].loc[
        :, ["date", "code", "future_1d_ret_tushare", "label_up_tushare"]
    ].copy()
    ranks = []
    for name in names:
        current = scored[name]
        ranks.append(
            current.groupby("date", sort=False)["score"].rank(pct=True).to_numpy()
        )
    base["score"] = np.mean(ranks, axis=0).astype("float32")
    return base


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Finite feature/parameter search on pure-Tushare Top15 rankers."
    )
    parser.add_argument("--feature-dir", type=Path, default=DEFAULT_FEATURE_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--predictions-path", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--target-precision", type=float, default=0.55)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    frame, features, data_audit = build_tushare_panel(args.feature_dir)
    validation_scores: dict[str, pd.DataFrame] = {}
    audits: list[dict[str, Any]] = []
    for index, config in enumerate(CONFIGS, start=1):
        print(f"[{index}/{len(CONFIGS)}] {config.name}", flush=True)
        model, scored, audit = _fit(frame, features, config, 2025)
        validation_scores[config.name] = scored
        audits.append(audit)
        print(f"precision={audit['metrics']['precision']:.6f}", flush=True)
        del model
        gc.collect()

    ordered = sorted(audits, key=lambda item: item["metrics"]["precision"], reverse=True)
    top_names = [item["config"]["name"] for item in ordered[:6]]
    ensemble_audits: list[dict[str, Any]] = []
    ensemble_scores: dict[tuple[str, ...], pd.DataFrame] = {}
    candidates = [(name,) for name in top_names]
    candidates.extend(itertools.combinations(top_names, 2))
    candidates.extend(tuple(top_names[:size]) for size in range(3, min(5, len(top_names)) + 1))
    for names in candidates:
        blended = _blend(validation_scores, tuple(names))
        metrics = _metrics(blended)
        ensemble_audits.append({"members": list(names), "metrics": metrics})
        ensemble_scores[tuple(names)] = blended
    selected = max(ensemble_audits, key=lambda item: item["metrics"]["precision"])
    selected_names = tuple(selected["members"])
    print(
        f"selected members={selected_names} validation={selected['metrics']['precision']:.6f}",
        flush=True,
    )

    holdout_scores: dict[str, pd.DataFrame] = {}
    checkpoints: list[str] = []
    args.model_dir.mkdir(parents=True, exist_ok=True)
    for name in selected_names:
        config = next(value for value in CONFIGS if value.name == name)
        model, scored, _ = _fit(frame, features, config, 2026)
        holdout_scores[name] = scored
        checkpoint = args.model_dir / f"tushare_search_{name}_through_2025.txt"
        model.save_model(checkpoint)
        checkpoints.append(str(checkpoint))
        del model
        gc.collect()
    holdout_blend = _blend(holdout_scores, selected_names)
    holdout_metrics = _metrics(holdout_blend)
    validation_blend = ensemble_scores[selected_names]
    predictions = pd.concat(
        [validation_blend.assign(split="validation_2025"), holdout_blend.assign(split="holdout_2026")],
        ignore_index=True,
    )
    args.predictions_path.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_parquet(args.predictions_path, index=False)
    validation_metrics = selected["metrics"]
    accepted = bool(
        validation_metrics["all_days_have_15"]
        and holdout_metrics["all_days_have_15"]
        and validation_metrics["precision"] >= args.target_precision
        and holdout_metrics["precision"] >= args.target_precision
    )
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accepted": accepted,
        "objective": "finite_pure_tushare_ranker_search_fixed_daily_top15",
        "target_precision": float(args.target_precision),
        "selection_count_per_day": 15,
        "selection_period": "2025 only",
        "data_contract": {
            "kronos_model_used": False,
            "kronos_prediction_file_used": False,
            "kronos_market_history_used": False,
            "download_performed": False,
            "uses_future_features": False,
            "audit": data_audit,
        },
        "individual_candidates": audits,
        "ensemble_candidates": ensemble_audits,
        "selected_members": list(selected_names),
        "validation_2025": validation_metrics,
        "holdout_2026": holdout_metrics,
        "checkpoints": checkpoints,
        "checkpoints_research_only": True,
        "promotion_decision": "not_evaluated" if accepted else "rejected",
        "disclaimer": DISCLAIMER,
    }
    _atomic_json(report, args.report_path)
    print(
        json.dumps(
            {
                "accepted": accepted,
                "selected_members": list(selected_names),
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
