from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest_rebalance import calculate_topk_rebalance, format_code_set
from config import (
    ACTIVE_RANKING_FEATURE_DIR,
    BACKTEST_DAILY_PREDICTIONS_PATH,
    BACKTEST_METRICS_PATH,
    BACKTEST_NAV_PATH,
    BACKTEST_TRADES_PATH,
)
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


DISCLAIMER = "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。"


def _maximum_drawdown(nav: pd.Series) -> float:
    if nav.empty:
        return 0.0
    drawdown = nav / nav.cummax() - 1.0
    return float(drawdown.min())


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _primary_validation(manifest: dict[str, Any]) -> dict[str, Any]:
    validation = dict(manifest.get("validation") or {})
    split = str(validation.get("primary_split") or "")
    return dict(validation.get(split) or {})


def _stock_names(feature_dir: Path) -> dict[str, str]:
    path = feature_dir / "stock_basic.csv"
    if not path.is_file():
        return {}
    header = pd.read_csv(path, nrows=0).columns.tolist()
    selected = [column for column in ("ts_code", "symbol", "name") if column in header]
    frame = pd.read_csv(path, usecols=selected, dtype=str)
    if "symbol" in frame:
        frame["code"] = frame["symbol"].astype(str).str.zfill(6)
    else:
        frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0].str.zfill(6)
    return (
        frame.dropna(subset=["code"])
        .drop_duplicates("code", keep="last")
        .set_index("code")["name"]
        .fillna("")
        .astype(str)
        .to_dict()
    )


def _next_cached_trade_date(feature_dir: Path, end_date: str) -> str:
    values = pd.read_csv(
        feature_dir / "daily_basic.csv",
        usecols=["trade_date"],
        dtype={"trade_date": str},
    )["trade_date"]
    dates = pd.to_datetime(
        values.astype(str).str[:8],
        format="%Y%m%d",
        errors="coerce",
    ).dropna().drop_duplicates().sort_values()
    later = dates[dates.gt(pd.Timestamp(end_date))]
    if later.empty:
        raise RuntimeError(f"回测结束日 {end_date} 之后没有缓存行情，无法计算下一交易日收益")
    return str(later.iloc[0].date())


def _atomic_csv(frame: pd.DataFrame, path_value: str | Path) -> None:
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, path)


def _atomic_json(value: dict[str, Any], path_value: str | Path) -> None:
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _signal_correlations(predictions: pd.DataFrame) -> dict[str, float | None]:
    ic_values: list[float] = []
    rank_ic_values: list[float] = []
    for _, group in predictions.groupby("date", sort=True):
        if len(group) < 3:
            continue
        score = group["model_score"]
        target = group["t1_ret"]
        ic = score.corr(target)
        rank_ic = score.rank().corr(target.rank())
        if pd.notna(ic):
            ic_values.append(float(ic))
        if pd.notna(rank_ic):
            rank_ic_values.append(float(rank_ic))

    def aggregate(values: list[float]) -> tuple[float | None, float | None]:
        if not values:
            return None, None
        series = pd.Series(values, dtype=float)
        mean = float(series.mean())
        deviation = float(series.std()) if len(series) > 1 else 0.0
        return mean, (mean / deviation if deviation > 1e-12 else None)

    ic_mean, icir = aggregate(ic_values)
    rank_ic_mean, rank_icir = aggregate(rank_ic_values)
    return {
        "ic_mean": ic_mean,
        "icir": icir,
        "rankic_mean": rank_ic_mean,
        "rankicir": rank_icir,
    }


def run_registered_ranker_backtest(
    *,
    topk: int = 15,
    backtest_days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    buy_cost: float = 0.0003,
    sell_cost: float = 0.0003,
    stamp_tax: float = 0.0005,
    feature_dir: str | Path = ACTIVE_RANKING_FEATURE_DIR,
    manifest_path: str | Path = ACTIVE_MODEL_MANIFEST_PATH,
    model_dir: str | Path = ACTIVE_MODEL_DIR,
) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    assets = validate_active_model_assets(
        manifest_path=manifest_path,
        model_dir=model_dir,
        verify_hashes=True,
    )
    if not assets.get("ready"):
        raise RuntimeError(f"主动排名模型资产未就绪：{assets}")

    manifest = load_active_model_manifest(manifest_path)
    validation = _primary_validation(manifest)
    requested_start = str(start_date or validation.get("start_date") or "2026-01-05")
    requested_end = str(end_date or validation.get("end_date") or "2026-07-30")
    feature_root = Path(feature_dir)
    label_end = _next_cached_trade_date(feature_root, requested_end)
    ensemble = RegisteredRankEnsemble.from_manifest(manifest, model_dir=model_dir)
    lookback = int((manifest.get("inference") or {}).get("lookback_trading_days") or 400)
    panel, features, audit = build_tushare_panel(
        feature_root,
        start_date=requested_start,
        end_date=label_end,
        required_features=ensemble.required_features,
        source_lookback_trading_days=lookback,
    )
    signal = panel[
        panel["date"].le(pd.Timestamp(requested_end))
        & panel["future_1d_ret_tushare"].notna()
    ].copy()
    available_dates = sorted(signal["date"].drop_duplicates().tolist())
    if backtest_days is not None and int(backtest_days) > 0:
        available_dates = available_dates[-int(backtest_days):]
        signal = signal[signal["date"].isin(available_dates)].copy()
    if not available_dates:
        raise RuntimeError("主动排名模型没有可回测的下一交易日标签")

    scored = ensemble.predict(signal)
    predictions = scored.merge(
        signal.loc[
            :,
            [
                "date", "code", "basic_log_close", "tech_ret_1", "tech_ret_5",
                "tech_ret_20", "tech_volatility_20", "tech_drawdown_20",
                "future_1d_ret_tushare", "label_up_tushare",
            ],
        ],
        on=["date", "code"],
        how="left",
        validate="one_to_one",
    )
    predictions["rank"] = predictions.groupby("date", sort=False)["model_score"].rank(
        ascending=False,
        method="first",
    ).astype(int)
    predictions["name"] = predictions["code"].map(_stock_names(feature_root)).fillna("")
    predictions["close"] = np.expm1(predictions["basic_log_close"].astype(float))
    predictions["day_ret"] = predictions["tech_ret_1"]
    predictions["t1_ret"] = predictions["future_1d_ret_tushare"]
    predictions["t1_up"] = predictions["label_up_tushare"].astype(int)
    predictions["score"] = predictions["model_score"]
    predictions["pred_score"] = predictions["model_score"]
    predictions["model_name"] = ACTIVE_MODEL_NAME
    predictions["model_backend"] = ACTIVE_MODEL_BACKEND
    predictions["model_version"] = ACTIVE_MODEL_VERSION
    ordered_dates = sorted(panel["date"].drop_duplicates().tolist())
    next_date = {
        date: ordered_dates[index + 1]
        for index, date in enumerate(ordered_dates[:-1])
    }
    predictions["prediction_date"] = predictions["date"].map(next_date)
    predictions = predictions.sort_values(["date", "rank"], kind="stable").reset_index(drop=True)

    selected = predictions[predictions["rank"].le(int(topk))].copy()
    expected_signals = len(available_dates) * int(topk)
    if len(selected) != expected_signals:
        raise RuntimeError(
            f"固定Top{topk}回测样本不完整：expected={expected_signals}, actual={len(selected)}"
        )

    nav_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    previous_holdings: set[str] = set()
    nav = 1.0
    benchmark_nav = 1.0
    for trade_date, group in predictions.groupby("date", sort=True):
        daily = group[group["rank"].le(int(topk))].sort_values("rank").copy()
        rebalance = calculate_topk_rebalance(previous_holdings, daily["code"].tolist())
        transaction_cost = (
            rebalance.buy_turnover * float(buy_cost)
            + rebalance.sell_turnover * (float(sell_cost) + float(stamp_tax))
        )
        gross_return = float(daily["t1_ret"].mean())
        net_return = gross_return - transaction_cost
        benchmark_return = float(group["t1_ret"].mean())
        nav *= 1.0 + net_return
        benchmark_nav *= 1.0 + benchmark_return
        nav_rows.append(
            {
                "date": str(pd.Timestamp(trade_date).date()),
                "prediction_date": str(pd.Timestamp(daily["prediction_date"].iloc[0]).date()),
                "gross_return": gross_return,
                "cost": transaction_cost,
                "net_return": net_return,
                "nav": nav,
                "benchmark_return": benchmark_return,
                "benchmark_nav": benchmark_nav,
                "selected_count": int(len(daily)),
                "turnover": rebalance.turnover,
                "buy_turnover": rebalance.buy_turnover,
                "sell_turnover": rebalance.sell_turnover,
                "bought_codes": format_code_set(rebalance.bought_codes),
                "sold_codes": format_code_set(rebalance.sold_codes),
                "model_name": ACTIVE_MODEL_NAME,
            }
        )
        daily["weight"] = 1.0 / len(daily)
        daily["portfolio_return"] = gross_return
        daily["net_portfolio_return"] = net_return
        daily["turnover"] = rebalance.turnover
        daily["rebalance_action"] = daily["code"].map(
            lambda code: "新买入" if code in rebalance.bought_codes else "继续持有"
        )
        trade_frames.append(daily)
        previous_holdings = rebalance.current_codes

    nav_frame = pd.DataFrame(nav_rows)
    trades = pd.concat(trade_frames, ignore_index=True)
    net_returns = nav_frame["net_return"].astype(float)
    excess_returns = net_returns - nav_frame["benchmark_return"].astype(float)
    periods = len(nav_frame)
    annualized_return = (
        float(nav_frame["nav"].iloc[-1] ** (252.0 / periods) - 1.0)
        if periods and nav_frame["nav"].iloc[-1] > 0
        else None
    )
    volatility = float(net_returns.std() * math.sqrt(252.0)) if periods > 1 else None
    sharpe = (
        float(net_returns.mean() / net_returns.std() * math.sqrt(252.0))
        if periods > 1 and net_returns.std() > 1e-12
        else None
    )
    information_ratio = (
        float(excess_returns.mean() / excess_returns.std() * math.sqrt(252.0))
        if periods > 1 and excess_returns.std() > 1e-12
        else None
    )
    daily_up_rate = selected.groupby("date")["t1_up"].mean()
    metrics: dict[str, Any] = {
        "mode": "registered_cross_sectional_daily_topk",
        "model_name": ACTIVE_MODEL_NAME,
        "model_backend": ACTIVE_MODEL_BACKEND,
        "model_version": ACTIVE_MODEL_VERSION,
        "ranking_basis": "two_member_daily_percentile_rank_fusion",
        "topk": int(topk),
        "holding_days": 1,
        "rebalance_frequency": "daily_t1",
        "periods": periods,
        "start_date": str(pd.Timestamp(nav_frame["date"].min()).date()),
        "end_date": str(pd.Timestamp(nav_frame["date"].max()).date()),
        "cumulative_return": float(nav_frame["nav"].iloc[-1] - 1.0),
        "benchmark_cumulative_return": float(nav_frame["benchmark_nav"].iloc[-1] - 1.0),
        "annualized_return": annualized_return,
        "annualized_volatility": volatility,
        "sharpe_ratio": sharpe,
        "information_ratio": information_ratio,
        "max_drawdown": _maximum_drawdown(nav_frame["nav"]),
        "win_rate": float((net_returns > 0).mean()),
        "average_turnover": float(nav_frame["turnover"].mean()),
        "mean_daily_return": float(nav_frame["gross_return"].mean()),
        "topk_mean_t1_return": float(selected["t1_ret"].mean()),
        "topk_daily_average_up_rate": float(daily_up_rate.mean()),
        "topk_up_count": int(selected["t1_up"].sum()),
        "topk_signal_count": int(len(selected)),
        "universe_up_rate": float(predictions["t1_up"].mean()),
        "target_precision": _finite((manifest.get("validation") or {}).get("target_precision")),
        "target_met": bool(float(daily_up_rate.mean()) >= float((manifest.get("validation") or {}).get("target_precision") or 0.55)),
        "buy_cost": float(buy_cost),
        "sell_cost": float(sell_cost),
        "stamp_tax": float(stamp_tax),
        "feature_count": len(features),
        "candidate_count": int(len(predictions)),
        "data_audit": audit,
        "disclaimer": DISCLAIMER,
        **_signal_correlations(predictions),
    }

    prediction_columns = [
        "date", "prediction_date", "rank", "code", "name", "close", "day_ret",
        "model_score", "member_1_rank_pct", "member_2_rank_pct", "t1_ret", "t1_up",
        "model_name", "model_backend", "model_version",
    ]
    trade_columns = [
        *prediction_columns, "weight", "portfolio_return", "net_portfolio_return",
        "turnover", "rebalance_action",
    ]
    _atomic_csv(predictions.loc[:, prediction_columns], BACKTEST_DAILY_PREDICTIONS_PATH)
    _atomic_csv(nav_frame, BACKTEST_NAV_PATH)
    _atomic_csv(trades.loc[:, trade_columns], BACKTEST_TRADES_PATH)
    _atomic_json(metrics, BACKTEST_METRICS_PATH)
    return nav_frame, metrics, trades.loc[:, trade_columns]


__all__ = ["run_registered_ranker_backtest"]
