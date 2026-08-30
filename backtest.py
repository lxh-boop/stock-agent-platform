import argparse
import json
import math
import os
from datetime import datetime

import numpy as np
import pandas as pd

from factor_provider import build_alpha158
from config import (
    BACKTEST_DAILY_PREDICTIONS_PATH,
    BACKTEST_METRICS_PATH,
    BACKTEST_NAV_PATH,
    BACKTEST_TRADES_PATH,
    DEFAULT_DFT_UNET_CHECKPOINT_PATH,
    LATEST_RAW_DATA_PATH,
    MODEL_PRED_COL,
    RAW_DATA_PATH,
    TRAIN_RAW_DATA_PATH,
    ensure_dirs,
)
import config as backtest_config
from data_tushare import fetch_stock_pool_recent_daily_fast
from market_context import ensure_market_context_for_feature_data
from model_zoo_backend import (
    is_zoo_backend,
    predict_zoo_scores_for_dates,
    zoo_model_name_from_backend,
)
from news_features import add_news_event_features
from universe import get_stock_pool
from backtest_rebalance import calculate_topk_rebalance, format_code_set

ENABLE_NEWS_FEATURES = getattr(backtest_config, "ENABLE_NEWS_FEATURES", True)
DEFAULT_BACKTEST_BACKEND = "zoo:chronos_bolt_small"
DFT_UNET_BACKEND = "dft_unet_external"


MIN_BACKTEST_DAYS = 60
FEATURE_WARMUP_DAYS = 70
DEFAULT_FETCH_TRADE_DAYS = 140


def calc_max_drawdown(nav: pd.Series) -> float:
    running_max = nav.cummax()
    drawdown = nav / running_max - 1.0
    return float(drawdown.min()) if not drawdown.empty else 0.0


def calc_ic(x: pd.Series, y: pd.Series) -> float:
    if len(x) < 3 or x.std() < 1e-12 or y.std() < 1e-12:
        return np.nan
    return float(np.corrcoef(x, y)[0, 1])


def calc_rankic(x: pd.Series, y: pd.Series) -> float:
    if len(x) < 3:
        return np.nan
    return float(x.rank().corr(y.rank()))


def normalize_raw_data(df: pd.DataFrame, stock_pool: dict) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["code"].isin(set(stock_pool))].copy()

    if df.empty:
        return df

    if "name" not in df.columns:
        df["name"] = df["code"].map(stock_pool)
    else:
        df["name"] = df["code"].map(stock_pool).fillna(df["name"])

    if "pct_chg" not in df.columns:
        df["pct_chg"] = np.nan

    if "vwap" not in df.columns:
        df["vwap"] = df["close"]

    if "turnover" not in df.columns:
        df["turnover"] = 0.0

    needed_cols = [
        "date",
        "code",
        "name",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "amount",
        "pct_chg",
        "vwap",
        "turnover",
    ]
    df = df[[c for c in needed_cols if c in df.columns]].copy()
    df = df.dropna(subset=["open", "close", "high", "low"])
    df = df.drop_duplicates(subset=["code", "date"], keep="last")
    df = df.sort_values(["code", "date"]).reset_index(drop=True)

    return df


def load_cached_raw_data(stock_pool: dict) -> pd.DataFrame:
    for path in [LATEST_RAW_DATA_PATH, TRAIN_RAW_DATA_PATH, RAW_DATA_PATH]:
        if not os.path.exists(path):
            continue

        df = pd.read_csv(path, dtype={"code": str})
        df = normalize_raw_data(df, stock_pool)

        if not df.empty:
            print(f"[Backtest Data] use cache: {path}, shape={df.shape}")
            return df

    return pd.DataFrame()


def merge_raw_data(old_df: pd.DataFrame, new_df: pd.DataFrame, stock_pool: dict) -> pd.DataFrame:
    if old_df is None or old_df.empty:
        data = new_df.copy()
    elif new_df is None or new_df.empty:
        data = old_df.copy()
    else:
        data = pd.concat([old_df, new_df], ignore_index=True)

    return normalize_raw_data(data, stock_pool)


def has_enough_recent_data(
    raw_data: pd.DataFrame,
    required_trade_days: int,
    min_stock_count: int = 250,
) -> bool:
    if raw_data.empty:
        return False

    dates = sorted(pd.to_datetime(raw_data["date"]).unique())

    if len(dates) < required_trade_days:
        return False

    recent_dates = dates[-required_trade_days:]
    recent = raw_data[raw_data["date"].isin(recent_dates)].copy()
    counts = recent.groupby("code")["date"].nunique()
    enough_stocks = int((counts >= int(required_trade_days * 0.8)).sum())

    print(
        "[Backtest Data] recent coverage: "
        f"dates={len(recent_dates)}, enough_stocks={enough_stocks}"
    )

    return enough_stocks >= min_stock_count


def ensure_latest_backtest_raw_data(
    token: str | None,
    stock_pool: dict,
    backtest_days: int,
    fetch_trade_days: int,
) -> tuple[pd.DataFrame, dict]:
    required_trade_days = max(backtest_days + FEATURE_WARMUP_DAYS + 1, 90)
    raw_data = load_cached_raw_data(stock_pool)

    if has_enough_recent_data(raw_data, required_trade_days):
        return raw_data, {
            "data_source_action": "cache",
            "required_trade_days": int(required_trade_days),
            "fetch_trade_days": 0,
            "raw_stock_count": int(raw_data["code"].nunique()),
            "raw_latest_date": str(pd.to_datetime(raw_data["date"].max()).date()),
        }

    if not token:
        raise RuntimeError(
            "本地最新行情不足以做最近 T+1 回测，请填写 Tushare Token 后下载最近行情。"
        )

    recent_trade_days = max(fetch_trade_days, required_trade_days)

    print(f"[Backtest Data] fetch recent {recent_trade_days} trade days for T+1 backtest")
    recent_raw = fetch_stock_pool_recent_daily_fast(
        token=token,
        stock_pool=stock_pool,
        recent_trade_days=recent_trade_days,
        include_turnover=False,
        sleep_seconds=0.05,
    )
    recent_raw = normalize_raw_data(recent_raw, stock_pool)
    raw_data = merge_raw_data(raw_data, recent_raw, stock_pool)
    raw_data.to_csv(LATEST_RAW_DATA_PATH, index=False, encoding="utf-8-sig")

    print(f"[Backtest Data] saved latest raw -> {LATEST_RAW_DATA_PATH}, shape={raw_data.shape}")

    if not has_enough_recent_data(raw_data, required_trade_days):
        raise RuntimeError("已下载最近行情，但数据覆盖仍不足以完成 T+1 回测。")

    return raw_data, {
        "data_source_action": "downloaded",
        "required_trade_days": int(required_trade_days),
        "fetch_trade_days": int(recent_trade_days),
        "raw_stock_count": int(raw_data["code"].nunique()),
        "raw_latest_date": str(pd.to_datetime(raw_data["date"].max()).date()),
    }


def add_t1_labels(feature_data: pd.DataFrame) -> pd.DataFrame:
    results = []

    for _, g in feature_data.groupby("code"):
        g = g.sort_values("date").copy()
        g["day_ret"] = g["close"].pct_change(1)
        g["t1_ret"] = g["close"].shift(-1) / g["close"] - 1.0
        g["t1_up"] = (g["t1_ret"] > 0).astype(int)
        results.append(g)

    data = pd.concat(results, ignore_index=True)
    data = data.replace([np.inf, -np.inf], np.nan)

    return data


def make_daily_predictions(
    feature_data: pd.DataFrame,
    raw_data: pd.DataFrame,
    model_version: str,
    backtest_days: int,
    model_backend: str = DEFAULT_BACKTEST_BACKEND,
    checkpoint_path: str | None = None,
    token: str | None = None,
) -> pd.DataFrame:
    pred_source = feature_data.dropna(subset=["close", "t1_ret"]).copy()
    dates = sorted(pred_source["date"].unique())

    if len(dates) < backtest_days:
        raise RuntimeError(
            f"可回测交易日不足 {backtest_days} 天，当前只有 {len(dates)} 天。"
        )

    use_dates = dates[-backtest_days:]

    model_backend = model_backend or DEFAULT_BACKTEST_BACKEND

    if model_backend == DFT_UNET_BACKEND:
        from external_models.dft_unet_adapter import DFTUNetAdapter

        feature_with_market, market_context_report = ensure_market_context_for_feature_data(
            feature_data=feature_data,
            token=token,
        )
        pred_with_labels = feature_with_market
        adapter = DFTUNetAdapter(
            checkpoint_path=checkpoint_path or DEFAULT_DFT_UNET_CHECKPOINT_PATH,
            device="cpu",
        ).load()
        pred_source = adapter.predict_scores_for_windows(
            feature_data=pred_with_labels,
            prediction_dates=use_dates,
        )
        pred_source = pred_source.dropna(subset=["t1_ret"]).copy()
        pred_source["model_name"] = "dft_unet_external"
        pred_source["model_backend"] = DFT_UNET_BACKEND
        pred_source["market_context_source"] = market_context_report.get("index_data", {}).get(
            "source",
            "cache",
        )
    elif is_zoo_backend(model_backend):
        zoo_model_name = zoo_model_name_from_backend(model_backend)
        pred_source = predict_zoo_scores_for_dates(
            model_name=zoo_model_name,
            raw_data=raw_data,
            feature_data=feature_data,
            prediction_dates=use_dates,
            device="cpu",
        )
        pred_source = pred_source.dropna(subset=["t1_ret"]).copy()
        pred_source["model_name"] = zoo_model_name
        pred_source["model_backend"] = model_backend
    else:
        raise RuntimeError(f"不支持的回测模型后端：{model_backend}")

    output_cols = [
        "date",
        "code",
        "name",
        "close",
        "day_ret",
        MODEL_PRED_COL,
        "up_prob",
        "score",
        "t1_ret",
        "t1_up",
        "ret_5",
        "ret_20",
        "vol_20",
        "drawdown_20",
        "model_name",
        "model_backend",
        "market_context_source",
    ]
    out = pred_source[[c for c in output_cols if c in pred_source.columns]].copy()
    out = out.sort_values(["date", "score"], ascending=[True, False]).reset_index(drop=True)
    out.to_csv(BACKTEST_DAILY_PREDICTIONS_PATH, index=False, encoding="utf-8-sig")

    print(
        f"[Save] T+1 daily predictions -> {BACKTEST_DAILY_PREDICTIONS_PATH}, "
        f"shape={out.shape}"
    )

    return out


def calc_signal_metrics(df: pd.DataFrame) -> dict:
    rows = []

    for date, g in df.groupby("date"):
        rows.append(
            {
                "date": date,
                "ic": calc_ic(g["score"], g["t1_ret"]),
                "rankic": calc_rankic(g["score"], g["t1_ret"]),
            }
        )

    daily = pd.DataFrame(rows)

    if daily.empty:
        return {
            "ic_mean": None,
            "icir": None,
            "rankic_mean": None,
            "rankicir": None,
        }

    ic_mean = daily["ic"].mean()
    ic_std = daily["ic"].std()
    rankic_mean = daily["rankic"].mean()
    rankic_std = daily["rankic"].std()

    return {
        "ic_mean": float(ic_mean) if not pd.isna(ic_mean) else None,
        "icir": float(ic_mean / ic_std)
        if ic_std and not pd.isna(ic_std) and ic_std > 1e-12
        else None,
        "rankic_mean": float(rankic_mean) if not pd.isna(rankic_mean) else None,
        "rankicir": float(rankic_mean / rankic_std)
        if rankic_std and not pd.isna(rankic_std) and rankic_std > 1e-12
        else None,
    }


def summarize_backtest(
    nav_df: pd.DataFrame,
    trades_df: pd.DataFrame,
    topk: int,
    costs: dict,
    model_version: str,
    model_backend: str,
    model_name: str,
) -> dict:
    returns = nav_df["net_ret"]
    periods = len(nav_df)
    periods_per_year = 252

    cumulative_return = float(nav_df["nav"].iloc[-1] - 1.0)
    benchmark_cumulative_return = float(nav_df["benchmark_nav"].iloc[-1] - 1.0)

    annualized_return = None
    if periods > 0 and nav_df["nav"].iloc[-1] > 0:
        annualized_return = float(nav_df["nav"].iloc[-1] ** (periods_per_year / periods) - 1.0)

    annualized_vol = float(returns.std() * math.sqrt(periods_per_year)) if periods > 1 else None
    sharpe = None

    if returns.std() and returns.std() > 1e-12:
        sharpe = float(returns.mean() / returns.std() * math.sqrt(periods_per_year))

    return {
        "mode": "latest_t1_daily",
        "model_name": model_name,
        "model_backend": model_backend,
        "model_version": model_version,
        "topk": int(topk),
        "holding_days": 1,
        "rebalance_frequency": "daily_t1",
        "periods": int(periods),
        "start_date": str(pd.to_datetime(nav_df["date"].min()).date()),
        "end_date": str(pd.to_datetime(nav_df["date"].max()).date()),
        "cumulative_return": cumulative_return,
        "benchmark_cumulative_return": benchmark_cumulative_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_vol,
        "sharpe_ratio": sharpe,
        "max_drawdown": calc_max_drawdown(nav_df["nav"]),
        "win_rate": float((returns > 0).mean()) if periods else None,
        "average_turnover": float(nav_df["turnover"].mean()) if periods else None,
        "mean_daily_return": float(nav_df["period_ret"].mean()) if periods else None,
        "topk_mean_t1_ret": float(trades_df["t1_ret"].mean()) if not trades_df.empty else None,
        "buy_cost": float(costs["buy_cost"]),
        "sell_cost": float(costs["sell_cost"]),
        "stamp_tax": float(costs["stamp_tax"]),
        "disclaimer": "本回测仅用于模型评估和项目展示，不构成投资建议，不用于实盘交易。",
    }


def run_latest_t1_backtest(
    token: str | None = None,
    model_version: str = "latest",
    model_backend: str = DEFAULT_BACKTEST_BACKEND,
    checkpoint_path: str | None = None,
    topk: int = 10,
    backtest_days: int = MIN_BACKTEST_DAYS,
    fetch_trade_days: int = DEFAULT_FETCH_TRADE_DAYS,
    buy_cost: float = 0.0003,
    sell_cost: float = 0.0003,
    stamp_tax: float = 0.0005,
) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    ensure_dirs()

    if backtest_days < MIN_BACKTEST_DAYS:
        backtest_days = MIN_BACKTEST_DAYS

    stock_pool = get_stock_pool(token=token, enrich_name=bool(token))
    raw_data, data_info = ensure_latest_backtest_raw_data(
        token=token,
        stock_pool=stock_pool,
        backtest_days=backtest_days,
        fetch_trade_days=fetch_trade_days,
    )

    feature_data = build_alpha158(raw_data)

    if ENABLE_NEWS_FEATURES:
        news_start_date = max(
            pd.to_datetime(raw_data["date"].min()),
            pd.to_datetime(raw_data["date"].max()) - pd.Timedelta(days=90),
        )
        feature_data = add_news_event_features(
            feature_data,
            stock_pool=stock_pool,
            token=token,
            refresh_cache=bool(token),
            start_date=news_start_date,
            end_date=raw_data["date"].max(),
        )

    feature_data = add_t1_labels(feature_data)
    daily_predictions = make_daily_predictions(
        feature_data=feature_data,
        raw_data=raw_data,
        model_version=model_version,
        backtest_days=backtest_days,
        model_backend=model_backend,
        checkpoint_path=checkpoint_path,
        token=token,
    )
    if model_backend == DFT_UNET_BACKEND:
        backtest_model_name = "dft_unet_external"
    elif is_zoo_backend(model_backend):
        backtest_model_name = zoo_model_name_from_backend(model_backend)
    else:
        raise RuntimeError(f"不支持的回测模型后端：{model_backend}")

    nav_rows = []
    trade_rows = []
    previous_holdings: set[str] = set()
    nav = 1.0
    benchmark_nav = 1.0
    costs = {
        "buy_cost": buy_cost,
        "sell_cost": sell_cost,
        "stamp_tax": stamp_tax,
    }

    for date, g in daily_predictions.groupby("date"):
        selected = g.sort_values("score", ascending=False).head(topk).copy()
        selected_count = len(selected)

        if selected_count == 0:
            continue

        rebalance = calculate_topk_rebalance(previous_holdings, selected["code"].tolist())
        current_holdings = rebalance.current_codes
        turnover = rebalance.turnover
        cost = (
            rebalance.buy_turnover * buy_cost
            + rebalance.sell_turnover * (sell_cost + stamp_tax)
        )

        period_ret = float(selected["t1_ret"].mean())
        net_ret = period_ret - cost
        benchmark_ret = float(g["t1_ret"].mean())

        nav *= 1.0 + net_ret
        benchmark_nav *= 1.0 + benchmark_ret

        nav_rows.append(
            {
                "date": date,
                "period_ret": period_ret,
                "cost": cost,
                "net_ret": net_ret,
                "nav": nav,
                "benchmark_ret": benchmark_ret,
                "benchmark_nav": benchmark_nav,
                "selected_count": selected_count,
                "turnover": turnover,
                "buy_turnover": rebalance.buy_turnover,
                "sell_turnover": rebalance.sell_turnover,
                "bought_codes": format_code_set(rebalance.bought_codes),
                "sold_codes": format_code_set(rebalance.sold_codes),
            }
        )

        selected = selected.reset_index(drop=True)
        selected["rank"] = np.arange(1, selected_count + 1)
        selected["weight"] = 1.0 / selected_count
        selected["portfolio_ret"] = period_ret
        selected["net_portfolio_ret"] = net_ret
        selected["turnover"] = turnover
        selected["buy_turnover"] = rebalance.buy_turnover
        selected["sell_turnover"] = rebalance.sell_turnover
        selected["rebalance_action"] = selected["code"].astype(str).str.zfill(6).map(
            lambda code: "新买入" if code in rebalance.bought_codes else "继续持有"
        )

        trade_cols = [
            "date",
            "rank",
            "code",
            "name",
            "weight",
            "close",
            "day_ret",
            MODEL_PRED_COL,
            "up_prob",
            "score",
            "t1_ret",
            "portfolio_ret",
            "net_portfolio_ret",
            "turnover",
            "buy_turnover",
            "sell_turnover",
            "rebalance_action",
            "ret_5",
            "ret_20",
            "vol_20",
            "drawdown_20",
            "model_name",
            "model_backend",
            "market_context_source",
        ]
        trade_rows.append(selected[[c for c in trade_cols if c in selected.columns]])

        previous_holdings = current_holdings

    nav_df = pd.DataFrame(nav_rows)

    if nav_df.empty:
        raise ValueError("没有生成任何 T+1 回测净值，请检查最新行情和模型。")

    trades_df = pd.concat(trade_rows, ignore_index=True) if trade_rows else pd.DataFrame()
    metrics = summarize_backtest(
        nav_df=nav_df,
        trades_df=trades_df,
        topk=topk,
        costs=costs,
        model_version=model_version,
        model_backend=model_backend,
        model_name=backtest_model_name,
    )
    metrics.update(data_info)
    metrics.update(calc_signal_metrics(daily_predictions))

    nav_df.to_csv(BACKTEST_NAV_PATH, index=False, encoding="utf-8-sig")
    trades_df.to_csv(BACKTEST_TRADES_PATH, index=False, encoding="utf-8-sig")

    with open(BACKTEST_METRICS_PATH, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    print(f"[Save] backtest nav -> {BACKTEST_NAV_PATH}, shape={nav_df.shape}")
    print(f"[Save] backtest trades -> {BACKTEST_TRADES_PATH}, shape={trades_df.shape}")
    print(f"[Save] backtest metrics -> {BACKTEST_METRICS_PATH}")
    print("[Backtest Metrics]")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))

    return nav_df, metrics, trades_df


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("--token", type=str, default=os.environ.get("TUSHARE_TOKEN", ""))
    parser.add_argument("--model-version", type=str, default="latest")
    parser.add_argument("--model-backend", type=str, default=DEFAULT_BACKTEST_BACKEND)
    parser.add_argument(
        "--checkpoint-path",
        type=str,
        default=DEFAULT_DFT_UNET_CHECKPOINT_PATH,
    )
    parser.add_argument("--topk", type=int, default=10)
    parser.add_argument("--backtest-days", type=int, default=MIN_BACKTEST_DAYS)
    parser.add_argument("--fetch-trade-days", type=int, default=DEFAULT_FETCH_TRADE_DAYS)
    parser.add_argument("--buy-cost", type=float, default=0.0003)
    parser.add_argument("--sell-cost", type=float, default=0.0003)
    parser.add_argument("--stamp-tax", type=float, default=0.0005)

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run_latest_t1_backtest(
        token=args.token.strip() or None,
        model_version=args.model_version,
        model_backend=args.model_backend,
        checkpoint_path=args.checkpoint_path,
        topk=args.topk,
        backtest_days=args.backtest_days,
        fetch_trade_days=args.fetch_trade_days,
        buy_cost=args.buy_cost,
        sell_cost=args.sell_cost,
        stamp_tax=args.stamp_tax,
    )
