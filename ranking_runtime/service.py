from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from confidence_scoring import add_confidence_scores
from config import ACTIVE_RANKING_FEATURE_DIR
from ranking_schema import normalize_ranking_columns, validate_ranking_schema
from risk_scoring import add_risk_scores
from stock_ranker.registered_ensemble import RegisteredRankEnsemble
from stock_ranker.tushare_panel import build_tushare_panel

from .settings import (
    ACTIVE_MODEL_BACKEND,
    ACTIVE_MODEL_DIR,
    ACTIVE_MODEL_MANIFEST_PATH,
    ACTIVE_MODEL_NAME,
    ACTIVE_MODEL_VERSION,
    load_active_model_manifest,
    validate_active_model_assets,
)


def _stock_metadata(feature_dir: Path) -> pd.DataFrame:
    path = feature_dir / "stock_basic.csv"
    if not path.is_file():
        return pd.DataFrame(columns=["code", "registered_name", "industry"])
    columns = pd.read_csv(path, nrows=0).columns.tolist()
    selected = [column for column in ("ts_code", "symbol", "name", "industry") if column in columns]
    frame = pd.read_csv(path, usecols=selected, dtype=str)
    if "symbol" in frame:
        frame["code"] = frame["symbol"].astype(str).str.zfill(6)
    else:
        frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0].str.zfill(6)
    frame["registered_name"] = frame.get("name", frame["code"]).fillna(frame["code"])
    if "industry" not in frame:
        frame["industry"] = ""
    return frame.loc[:, ["code", "registered_name", "industry"]].drop_duplicates(
        "code", keep="last"
    )


def _market_context(feature_data: pd.DataFrame | None, signal_date: str) -> pd.DataFrame:
    desired = [
        "code",
        "name",
        "close",
        "amount",
        "volume",
        "pct_chg",
        "ret_5",
        "ret_20",
        "vol_20",
        "drawdown_20",
    ]
    if feature_data is None or feature_data.empty:
        return pd.DataFrame(columns=desired)
    frame = feature_data.copy()
    if "date" in frame:
        dates = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame[dates.eq(pd.Timestamp(signal_date))]
    if frame.empty or "code" not in frame:
        return pd.DataFrame(columns=desired)
    frame["code"] = frame["code"].astype(str).str.split(".").str[0].str.zfill(6)
    columns = [column for column in desired if column in frame.columns]
    return frame.loc[:, columns].drop_duplicates("code", keep="last")


def _attach_context(
    scored: pd.DataFrame,
    panel: pd.DataFrame,
    *,
    feature_dir: Path,
    feature_data: pd.DataFrame | None,
    signal_date: str,
) -> pd.DataFrame:
    feature_columns = [
        column
        for column in (
            "basic_log_close",
            "tech_ret_5",
            "tech_ret_20",
            "tech_volatility_20",
            "tech_drawdown_20",
        )
        if column in panel.columns
    ]
    out = scored.merge(
        panel.loc[:, ["date", "code", *feature_columns]],
        on=["date", "code"],
        how="left",
        validate="one_to_one",
    )
    out = out.merge(_stock_metadata(feature_dir), on="code", how="left", validate="one_to_one")
    market = _market_context(feature_data, signal_date)
    if not market.empty:
        out = out.merge(market, on="code", how="left", validate="one_to_one")
    out["name"] = out.get("name", pd.Series(index=out.index, dtype=object)).fillna(
        out.get("registered_name", pd.Series(index=out.index, dtype=object))
    ).fillna(out["code"])
    inferred_close = np.expm1(pd.to_numeric(out.get("basic_log_close"), errors="coerce"))
    if "close" not in out:
        out["close"] = inferred_close
    else:
        out["close"] = pd.to_numeric(out["close"], errors="coerce").fillna(inferred_close)
    aliases = {
        "ret_5": "tech_ret_5",
        "ret_20": "tech_ret_20",
        "vol_20": "tech_volatility_20",
        "drawdown_20": "tech_drawdown_20",
    }
    for target, source in aliases.items():
        fallback = pd.to_numeric(out.get(source), errors="coerce")
        if target not in out:
            out[target] = fallback
        else:
            out[target] = pd.to_numeric(out[target], errors="coerce").fillna(fallback)
    return out


def _primary_validation(manifest: dict[str, Any]) -> dict[str, Any]:
    validation = dict(manifest.get("validation") or {})
    split = str(validation.get("primary_split") or "")
    return dict(validation.get(split) or {})


def _rank_bucket_rate(rank: pd.Series, validation: dict[str, Any]) -> pd.Series:
    top5 = float(validation.get("top5_precision") or np.nan)
    top10 = float(validation.get("top10_precision") or np.nan)
    top15 = float(validation.get("top15_precision") or np.nan)
    base = float(validation.get("universe_up_rate") or np.nan)
    second = (top10 * 10.0 - top5 * 5.0) / 5.0
    third = (top15 * 15.0 - top10 * 10.0) / 5.0
    return pd.Series(
        np.select(
            [rank.le(5), rank.le(10), rank.le(15)],
            [top5, second, third],
            default=base,
        ),
        index=rank.index,
        dtype=float,
    )


def generate_active_ranking(
    *,
    signal_date: str,
    prediction_date: str,
    feature_data: pd.DataFrame | None = None,
    feature_dir: str | Path = ACTIVE_RANKING_FEATURE_DIR,
    manifest_path: str | Path = ACTIVE_MODEL_MANIFEST_PATH,
    model_dir: str | Path = ACTIVE_MODEL_DIR,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    assets = validate_active_model_assets(
        manifest_path=manifest_path,
        model_dir=model_dir,
        verify_hashes=True,
    )
    if not assets.get("ready"):
        raise RuntimeError(f"主动排名模型资产未就绪：{assets}")
    manifest = load_active_model_manifest(manifest_path)
    ensemble = RegisteredRankEnsemble.from_manifest(manifest, model_dir=model_dir)
    lookback = int((manifest.get("inference") or {}).get("lookback_trading_days") or 400)
    panel, features, data_audit = build_tushare_panel(
        feature_dir,
        start_date=signal_date,
        end_date=signal_date,
        required_features=ensemble.required_features,
        source_lookback_trading_days=lookback,
    )
    minimum_candidates = int((manifest.get("inference") or {}).get("minimum_candidates") or 15)
    if len(panel) < minimum_candidates:
        raise RuntimeError(
            f"主动排名模型候选不足：signal_date={signal_date}, "
            f"expected>={minimum_candidates}, actual={len(panel)}"
        )

    scored = ensemble.predict(panel)
    out = _attach_context(
        scored,
        panel,
        feature_dir=Path(feature_dir),
        feature_data=feature_data,
        signal_date=signal_date,
    )
    out = out.sort_values(
        ["model_score", "code"], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)
    out.insert(0, "rank", np.arange(1, len(out) + 1))
    out["date"] = str(pd.Timestamp(signal_date).date())
    out["prediction_date"] = str(pd.Timestamp(prediction_date).date())
    out["prediction_for_date"] = out["prediction_date"]
    out["pred_score"] = out["model_score"]
    out["score"] = out["model_score"]
    out["raw_score"] = out["model_score"]
    out["pred_return"] = np.nan
    out["pred_5d_ret"] = np.nan
    out["up_prob"] = 0.5
    out["up_prob_calibrated"] = np.nan
    out["calibrated"] = False
    out["calibration_method"] = "not_an_individual_probability"
    out["model_name"] = ACTIVE_MODEL_NAME
    out["model_backend"] = ACTIVE_MODEL_BACKEND
    out["model_version"] = ACTIVE_MODEL_VERSION
    selection_count = int(manifest.get("selection_count") or 15)
    out["top15_up_signal"] = out["rank"].le(selection_count)

    validation = _primary_validation(manifest)
    out["historical_rank_bucket_up_rate"] = _rank_bucket_rate(out["rank"], validation)
    out["top5_daily_average_up_rate"] = validation.get("top5_precision")
    out["top10_daily_average_up_rate"] = validation.get("top10_precision")
    out["top15_daily_average_up_rate"] = validation.get("top15_precision")
    out["top15_observation_days"] = validation.get("days")
    out["top15_complete_days"] = validation.get("days")
    out["top15_observation_count"] = validation.get("top15_signals")
    out["top15_rise_count"] = validation.get("top15_correct")
    out["top15_start_date"] = validation.get("start_date")
    out["top15_end_date"] = validation.get("end_date")
    out["calibration_top_k"] = selection_count
    out["calibration_target"] = "next_trading_day_close_up"

    out = add_risk_scores(out)
    out = add_confidence_scores(
        out,
        calibration_report={
            "calibrated": False,
            "reason": "rank_score_is_not_an_individual_up_probability",
        },
    )
    out = normalize_ranking_columns(out)
    validate_ranking_schema(out)
    report = {
        "source": "registered_cross_sectional_ranker",
        "model_name": ACTIVE_MODEL_NAME,
        "model_backend": ACTIVE_MODEL_BACKEND,
        "model_version": ACTIVE_MODEL_VERSION,
        "engine": str(manifest.get("engine") or ""),
        "fusion_method": str((manifest.get("fusion") or {}).get("method") or ""),
        "member_count": len(ensemble.members),
        "member_feature_counts": [len(member.features) for member in ensemble.members],
        "feature_union_count": len(features),
        "ranking_basis": "daily_cross_sectional_percentile_rank_fusion",
        "individual_up_probability_available": False,
        "fixed_top15_count": int(out["top15_up_signal"].sum()),
        "candidate_count": len(out),
        "data_audit": data_audit,
        "assets": assets,
        "training": manifest.get("training") or {},
        "validation": manifest.get("validation") or {},
    }
    return out, report


__all__ = ["generate_active_ranking"]
