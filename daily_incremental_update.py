from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

from factor_provider import build_alpha158
from config import (
    ACTIVE_RANKING_MODEL_METRICS_PATH,
    ENABLE_NEWS_FEATURES,
    LATEST_RAW_DATA_PATH,
    OUTPUT_DIR,
    RANKING_LATEST_PATH,
    RAW_DATA_PATH,
    TRAIN_RAW_DATA_PATH,
    ensure_dirs,
)
from data_tushare import (
    build_market_data_window,
    fetch_stock_pool_recent_daily_fast,
    get_token,
    init_tushare_pro,
)
from database.repositories import PredictionRepository
from news_db_sync import sync_event_cache_to_agent_db
from news_features import add_news_event_features
from pipelines.daily_update_pipeline import run_daily_update_pipeline
from pipelines.schemas import PipelineContext
from portfolio.paper_market_data import merge_paper_market_ohlc
from ranking_runtime import (
    ACTIVE_MODEL_BACKEND,
    ACTIVE_MODEL_NAME,
    ACTIVE_MODEL_VERSION,
    generate_active_ranking,
)
from ranking_runtime.tushare_features import refresh_ranker_features_for_date
from universe import get_stock_pool


DISCLAIMER = "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。"
FETCH_RECENT_TRADE_DAYS = 10
DEFAULT_EXTERNAL_MODEL_BACKEND = ACTIVE_MODEL_BACKEND


def _persist_runtime_ranking(ranking: pd.DataFrame) -> int:
    records = ranking.where(pd.notna(ranking), None).to_dict(orient="records")
    db_path = os.environ.get("STOCK_AGENT_DB_PATH") or None
    persisted = PredictionRepository(db_path).replace_snapshot(
        records,
        source_kind="ranking",
        exclusive_scope=True,
    )
    if len(persisted) != len(records):
        raise RuntimeError(
            "runtime_ranking_persistence_count_mismatch:"
            f"source={len(records)}:persisted={len(persisted)}"
        )
    return len(persisted)


def normalize_raw_data(df: pd.DataFrame, stock_pool: dict[str, str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    frame = df.copy()
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[frame["code"].isin(set(stock_pool))].copy()
    mapped_names = frame["code"].map(stock_pool)
    if "name" not in frame.columns:
        frame["name"] = mapped_names
    else:
        frame["name"] = mapped_names.fillna(frame["name"])
    if "pct_chg" not in frame.columns:
        frame["pct_chg"] = np.nan
    if "vwap" not in frame.columns:
        frame["vwap"] = frame["close"]
    if "turnover" not in frame.columns:
        frame["turnover"] = 0.0
    needed = [
        "date", "code", "name", "open", "close", "high", "low",
        "volume", "amount", "pct_chg", "vwap", "turnover", "adj_factor",
    ]
    frame = frame[[column for column in needed if column in frame.columns]]
    return (
        frame.dropna(subset=["open", "close", "high", "low"])
        .sort_values(["code", "date"])
        .reset_index(drop=True)
    )


def load_cached_raw_data(stock_pool: dict[str, str]) -> pd.DataFrame:
    for path in (LATEST_RAW_DATA_PATH, TRAIN_RAW_DATA_PATH, RAW_DATA_PATH):
        if not os.path.exists(path):
            continue
        frame = normalize_raw_data(pd.read_csv(path, dtype={"code": str}), stock_pool)
        if not frame.empty:
            print(f"[Data] use local cache: {path}")
            return frame
        print(f"[Data] cache empty after universe filter: {path}")
    return pd.DataFrame()


def merge_raw_data(
    old_df: pd.DataFrame,
    new_df: pd.DataFrame,
    stock_pool: dict[str, str],
) -> pd.DataFrame:
    if old_df is None or old_df.empty:
        data = new_df.copy()
    elif new_df is None or new_df.empty:
        data = old_df.copy()
    else:
        data = pd.concat([old_df, new_df], ignore_index=True)
    data["code"] = data["code"].astype(str).str.zfill(6)
    data["date"] = pd.to_datetime(data["date"])
    data = data.drop_duplicates(["code", "date"], keep="last")
    return normalize_raw_data(data, stock_pool)


def refresh_news_cache_and_sync_db(
    feature_data: pd.DataFrame,
    raw_data: pd.DataFrame,
    stock_pool: dict[str, str],
    token: str | None,
    refresh_cache: bool = True,
) -> dict:
    if not ENABLE_NEWS_FEATURES or feature_data.empty or raw_data.empty:
        return {"enabled": False, "reason": "news_features_disabled_or_empty_data"}
    news_start_date = max(
        pd.to_datetime(raw_data["date"].min()),
        pd.to_datetime(raw_data["date"].max()) - pd.Timedelta(days=90),
    )
    news_end_date = pd.to_datetime(raw_data["date"].max())
    if refresh_cache:
        add_news_event_features(
            feature_data.copy(),
            stock_pool=stock_pool,
            token=token,
            refresh_cache=True,
            start_date=news_start_date,
            end_date=news_end_date,
        )
        print("[News] cache refreshed; ranking model inputs remain market features only.")
    status = sync_event_cache_to_agent_db(
        stock_pool=stock_pool,
        db_path=None,
        output_dir=OUTPUT_DIR,
        start_date=news_start_date.strftime("%Y-%m-%d"),
        end_date=news_end_date.strftime("%Y-%m-%d"),
    ).to_dict()
    print(f"[News DB] sync status = {status}")
    return status


def prepare_latest_feature_data(
    token: str,
    include_news_features: bool = True,
    sync_news_db: bool = True,
    fetch_workers: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp, dict[str, str]]:
    stock_pool = get_stock_pool(token=token, enrich_name=True)
    print(f"[Universe] update stock count = {len(stock_pool)}")
    old_raw = load_cached_raw_data(stock_pool)
    if not old_raw.empty:
        print(f"[Data] cached raw shape = {old_raw.shape}")
        print(f"[Data] cached date range = {old_raw['date'].min()} ~ {old_raw['date'].max()}")
    recent_raw = fetch_stock_pool_recent_daily_fast(
        token=token,
        stock_pool=stock_pool,
        recent_trade_days=FETCH_RECENT_TRADE_DAYS,
        include_adj_factor=True,
        max_workers=fetch_workers,
    )
    recent_raw = normalize_raw_data(recent_raw, stock_pool)
    if recent_raw.empty:
        raise RuntimeError("Tushare 最近行情为空，无法生成最新排名。")
    new_data_start_date = recent_raw["date"].min()
    raw_data = merge_raw_data(old_raw, recent_raw, stock_pool)
    raw_data.to_csv(LATEST_RAW_DATA_PATH, index=False, encoding="utf-8-sig")
    print(f"[Data] merged raw shape = {raw_data.shape}")
    print(f"[Data] merged date range = {raw_data['date'].min()} ~ {raw_data['date'].max()}")

    # Stage 3.7: Alpha158 is a derived in-memory view of the current raw data.
    # No factor CSV is read or written, and build_alpha158 has no factor cache.
    feature_data = build_alpha158(raw_data)
    if ENABLE_NEWS_FEATURES and include_news_features:
        news_start_date = max(
            pd.to_datetime(raw_data["date"].min()),
            pd.to_datetime(raw_data["date"].max()) - pd.Timedelta(days=90),
        )
        feature_data = add_news_event_features(
            feature_data,
            stock_pool=stock_pool,
            token=token,
            refresh_cache=True,
            start_date=news_start_date,
            end_date=raw_data["date"].max(),
        )
    print(f"[Factor] Alpha158 recomputed in memory, rows={len(feature_data)}")
    if sync_news_db:
        refresh_news_cache_and_sync_db(
            feature_data,
            raw_data,
            stock_pool,
            token,
            refresh_cache=not (ENABLE_NEWS_FEATURES and include_news_features),
        )
    return feature_data, raw_data, new_data_start_date, stock_pool


def _historical_validation(ranking_report: dict) -> dict:
    validation = dict(ranking_report.get("validation") or {})
    primary_split = str(validation.get("primary_split") or "")
    primary = dict(validation.get(primary_split) or {})
    training = dict(ranking_report.get("training") or {})
    baseline = float(primary.get("universe_up_rate") or 0.0)
    top5 = float(primary.get("top5_precision") or 0.0)
    top10 = float(primary.get("top10_precision") or 0.0)
    top15 = float(primary.get("top15_precision") or 0.0)
    return {
        "valid_test_days": int(primary.get("days") or 0),
        "test_start_date": str(primary.get("start_date") or ""),
        "test_end_date": str(primary.get("end_date") or ""),
        "train_end_date": str(training.get("trained_through") or ""),
        "best_epoch": None,
        "universe_next_day_up_probability": baseline,
        "top5_next_day_up_probability": top5,
        "top10_next_day_up_probability": top10,
        "top15_next_day_up_probability": top15,
        "top5_lift_vs_universe": top5 - baseline,
        "top10_lift_vs_universe": top10 - baseline,
        "top15_lift_vs_universe": top15 - baseline,
        "all_topk_above_universe": all(value > baseline for value in (top5, top10, top15)),
        "target_precision": float(validation.get("target_precision") or 0.55),
        "target_met": bool(validation.get("target_met", False)),
        "promotion_status": str(validation.get("promotion_status") or ""),
    }


def active_ranker_daily_update(
    token: str | None = None,
    fetch_workers: int | None = None,
) -> tuple[pd.DataFrame, dict]:
    token = get_token(token)
    ensure_dirs()
    print("=" * 80)
    print("[Active Ranker Daily Update]", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 80)
    feature_data, _raw_data, _, stock_pool = prepare_latest_feature_data(
        token,
        include_news_features=False,
        sync_news_db=False,
        fetch_workers=fetch_workers,
    )
    market_window = build_market_data_window(init_tushare_pro(token))
    signal_date = str(market_window["expected_signal_date"])
    prediction_date = str(market_window["prediction_target_date"])
    if _raw_data is not None and not _raw_data.empty and "date" in _raw_data.columns:
        raw_dates = pd.to_datetime(_raw_data["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        merge_paper_market_ohlc(
            _raw_data[raw_dates.eq(signal_date)],
            source="daily_incremental_update",
        )
    feature_refresh = refresh_ranker_features_for_date(
        token=token,
        signal_date=signal_date,
        stock_codes=set(stock_pool),
    )
    if not feature_refresh.get("ready"):
        raise RuntimeError(
            f"主动排名所需 Tushare 特征未就绪：{feature_refresh}"
        )
    ranking, ranking_report = generate_active_ranking(
        signal_date=signal_date,
        prediction_date=prediction_date,
        feature_data=feature_data,
    )
    persisted_count = _persist_runtime_ranking(ranking)
    ranking.to_csv(RANKING_LATEST_PATH, index=False, encoding="utf-8-sig")
    date_text = signal_date.replace("-", "")[:8]
    dated_path = os.path.join(OUTPUT_DIR, f"ranking_{date_text}_{ACTIVE_MODEL_NAME}.csv")
    ranking.to_csv(dated_path, index=False, encoding="utf-8-sig")

    metrics = {
        "model_name": ACTIVE_MODEL_NAME,
        "model_backend": ACTIVE_MODEL_BACKEND,
        "model_version": ACTIVE_MODEL_VERSION,
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "update_type": "daily_registered_cross_sectional_ranking",
        "prediction_horizon": "next_trading_day_T_plus_1",
        "prediction_signal_date": signal_date,
        "prediction_date": prediction_date,
        "ranking_rows": int(len(ranking)),
        "database_ranking_rows": persisted_count,
        "runtime_data_authority": "database/model_prediction",
        "model_feature_source": "local_tushare_cross_sectional_panel",
        "model_native_output": "cross_sectional_rank_score",
        "ranking_basis": "two_member_daily_percentile_rank_fusion",
        "individual_up_probability_available": False,
        "feature_refresh": feature_refresh,
        "ranking": ranking_report,
        "historical_validation": _historical_validation(ranking_report),
        "disclaimer": DISCLAIMER,
    }
    metrics_path = os.path.abspath(ACTIVE_RANKING_MODEL_METRICS_PATH)
    os.makedirs(os.path.dirname(metrics_path), exist_ok=True)
    temporary = metrics_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(metrics, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(temporary, metrics_path)
    print(f"[Save] latest ranking -> {RANKING_LATEST_PATH}")
    print(f"[Save] dated ranking -> {dated_path}")
    print(f"[Save] active model metrics -> {metrics_path}")
    print(ranking.head(15)[["rank", "code", "name", "model_score"]])
    return ranking, metrics


def run_post_prediction_ai_adjustment(
    user_id: str = "default",
    top_k: int = 50,
    output_dir: str = OUTPUT_DIR,
    db_path: str | None = None,
    paper_trading_enabled: bool = False,
    dry_run: bool = False,
):
    context = PipelineContext(
        user_id=user_id,
        trade_date="latest",
        top_k=int(top_k),
        output_dir=output_dir,
        db_path=db_path,
        dry_run=bool(dry_run),
        paper_trading_enabled=bool(paper_trading_enabled),
    )
    steps = ["prediction", "rag", "scoring"]
    if paper_trading_enabled:
        steps.append("paper")
    steps.append("report")
    return run_daily_update_pipeline(context, steps)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", type=str, default="", help="可选；默认读取本地配置或环境变量。")
    parser.add_argument("--base-version", type=str, default="latest", help="兼容参数。")
    parser.add_argument(
        "--model-backend",
        type=str,
        default=DEFAULT_EXTERNAL_MODEL_BACKEND,
        help="稳定的主动排名模型后端别名。",
    )
    parser.add_argument("--checkpoint-path", type=str, default="", help="兼容参数；模型资产由注册清单解析。")
    parser.add_argument("--fetch-workers", type=int, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backend = str(args.model_backend or DEFAULT_EXTERNAL_MODEL_BACKEND).strip()
    if backend != ACTIVE_MODEL_BACKEND:
        raise ValueError(
            f"仅支持主动排名模型后端 {ACTIVE_MODEL_BACKEND}；actual={backend}"
        )
    active_ranker_daily_update(token=args.token or None, fetch_workers=args.fetch_workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
