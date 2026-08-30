from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest_metrics import (  # noqa: E402
    calc_annual_return,
    calc_annual_volatility,
    calc_max_drawdown,
    calc_sharpe,
    calc_win_rate,
    summarize_ic,
)
from config import LATEST_RAW_DATA_PATH  # noqa: E402
from factor_provider import build_alpha158  # noqa: E402
from model_zoo.metadata import get_model_metadata  # noqa: E402
from model_zoo.registry import get_model_entry  # noqa: E402
from model_zoo_backend import predict_zoo_scores_for_dates  # noqa: E402
from backtest_rebalance import calculate_topk_rebalance, format_code_set  # noqa: E402


DISCLAIMER = "回测结果仅代表历史数据上的模型表现，不代表未来收益，不构成投资建议。"
BACKTEST_ROOT = Path("outputs") / "backtests"
DAILY_RETURNS_DIR = BACKTEST_ROOT / "daily_returns"
PREDICTIONS_DIR = BACKTEST_ROOT / "predictions"
METRICS_DIR = BACKTEST_ROOT / "metrics"
MASTER_TABLE_PATH = BACKTEST_ROOT / "backtest_master_table.csv"

DAILY_RETURN_COLUMNS = [
    "date",
    "run_id",
    "model_name",
    "model_source",
    "model_category",
    "topk",
    "holding_days",
    "rank_by",
    "selected_codes",
    "selected_names",
    "mean_pred_5d_ret",
    "mean_future_5d_ret",
    "gross_return",
    "turnover",
    "buy_turnover",
    "sell_turnover",
    "bought_codes",
    "sold_codes",
    "cost",
    "net_return",
    "cum_return",
    "nav",
    "benchmark_return",
    "excess_return",
]

MASTER_COLUMNS = [
    "run_id",
    "timestamp",
    "model_name",
    "model_source",
    "model_category",
    "has_pretrained_weight",
    "trained_locally",
    "checkpoint_path",
    "repo_url",
    "commit_hash",
    "adapter_name",
    "topk",
    "holding_days",
    "rank_by",
    "cost_mode",
    "start_date",
    "end_date",
    "num_days",
    "annual_return",
    "cum_return",
    "final_nav",
    "annual_volatility",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "mean_daily_return",
    "mean_turnover",
    "mean_cost",
    "IC",
    "RankIC",
    "ICIR",
    "RankICIR",
    "target_metric",
    "target_value",
    "target_hit",
    "daily_returns_csv",
    "prediction_csv",
    "metrics_json",
    "status",
    "fail_reason",
    "notes",
]


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if np.isfinite(value) else None
    if pd.isna(value) if not isinstance(value, (list, tuple, dict)) else False:
        return None
    return value


def ensure_dirs() -> None:
    DAILY_RETURNS_DIR.mkdir(parents=True, exist_ok=True)
    PREDICTIONS_DIR.mkdir(parents=True, exist_ok=True)
    METRICS_DIR.mkdir(parents=True, exist_ok=True)


def parse_topk_list(value: str) -> list[int]:
    out = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        out.append(int(item))
    if not out:
        raise ValueError("topk list is empty")
    return sorted(set(out))


def load_cached_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    raw_path = Path(LATEST_RAW_DATA_PATH)
    if not raw_path.exists():
        raise FileNotFoundError(f"missing raw data cache: {raw_path}")

    raw = pd.read_csv(raw_path, dtype={"code": str}, encoding="utf-8-sig")
    raw["date"] = pd.to_datetime(raw["date"])
    raw["code"] = raw["code"].astype(str).str.zfill(6)
    feature = build_alpha158(raw, output_trading_days=120)
    return raw, feature


def select_prediction_dates(
    feature_data: pd.DataFrame,
    backtest_days: int,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[pd.Timestamp]:
    if "future_5d_ret" not in feature_data.columns:
        raise RuntimeError("feature data does not contain future_5d_ret for historical backtest")
    data = feature_data.dropna(subset=["future_5d_ret"]).copy()
    dates = pd.Series(pd.to_datetime(data["date"].unique())).sort_values()
    if start_date:
        dates = dates[dates >= pd.to_datetime(start_date)]
    if end_date:
        dates = dates[dates <= pd.to_datetime(end_date)]
    dates = dates.tail(int(backtest_days))
    if dates.empty:
        raise RuntimeError("no labeled prediction dates available")
    return list(dates)


def normalize_predictions(pred: pd.DataFrame, model_name: str, rank_by: str) -> pd.DataFrame:
    if pred is None or pred.empty:
        raise RuntimeError(f"{model_name} produced empty predictions")
    out = pred.copy()
    out["date"] = pd.to_datetime(out["date"])
    out["code"] = out["code"].astype(str).str.zfill(6)
    if "name" not in out.columns:
        out["name"] = out["code"]
    if "pred_5d_ret" not in out.columns:
        if "raw_score" in out.columns:
            out["pred_5d_ret"] = out["raw_score"]
        else:
            raise RuntimeError("prediction frame missing pred_5d_ret/raw_score")
    if "raw_score" not in out.columns:
        out["raw_score"] = out["pred_5d_ret"]
    if "score" not in out.columns:
        out["score"] = out.groupby("date")["raw_score"].rank(pct=True)
    if rank_by not in out.columns:
        raise RuntimeError(f"rank_by column not found: {rank_by}")
    if "future_5d_ret" not in out.columns:
        raise RuntimeError("prediction frame missing future_5d_ret for backtest")

    if "future_1d_ret" not in out.columns and "close" in out.columns:
        out = out.sort_values(["code", "date"]).copy()
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        out["future_1d_ret"] = out.groupby("code")["close"].shift(-1) / out["close"] - 1.0

    numeric_cols = ["pred_5d_ret", "raw_score", "score", rank_by, "future_5d_ret"]
    if "future_1d_ret" in out.columns:
        numeric_cols.append("future_1d_ret")

    for col in numeric_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    out = out.dropna(subset=[rank_by, "future_5d_ret"])
    return out.sort_values(["date", rank_by], ascending=[True, False]).reset_index(drop=True)


def realized_return_column_for_holding(pred: pd.DataFrame, holding_days: int) -> str:
    if int(holding_days) == 1:
        if "future_1d_ret" not in pred.columns:
            raise RuntimeError("holding_days=1 需要 future_1d_ret，当前预测结果无法做 T+1 回测。")
        return "future_1d_ret"
    return "future_5d_ret"


def build_daily_returns(
    pred: pd.DataFrame,
    run_id: str,
    model_name: str,
    model_source: str,
    model_category: str,
    topk: int,
    holding_days: int,
    rank_by: str,
    cost_rate: float | None = None,
    buy_cost: float = 0.0003,
    sell_cost: float = 0.0003,
    stamp_tax: float = 0.0005,
) -> pd.DataFrame:
    rows = []
    previous_codes: set[str] = set()
    nav = 1.0
    realized_col = realized_return_column_for_holding(pred, holding_days)
    pred = pred.dropna(subset=[realized_col]).copy()
    if pred.empty:
        raise RuntimeError(f"no realized returns available for holding_days={holding_days}")

    dates = pd.Series(pd.to_datetime(pred["date"].unique())).sort_values().tolist()
    rebalance_dates = dates[:: max(int(holding_days), 1)]

    for date in rebalance_dates:
        group = pred[pred["date"] == date].copy()
        selected = group.sort_values([rank_by, "raw_score"], ascending=[False, False]).head(int(topk)).copy()
        if selected.empty:
            continue
        codes = selected["code"].astype(str).str.zfill(6).tolist()
        names = selected["name"].astype(str).tolist()
        rebalance = calculate_topk_rebalance(previous_codes, codes)
        current_codes = rebalance.current_codes
        if cost_rate is None:
            cost = float(
                rebalance.buy_turnover * buy_cost
                + rebalance.sell_turnover * (sell_cost + stamp_tax)
            )
        else:
            cost = float((rebalance.buy_turnover + rebalance.sell_turnover) * cost_rate)
        gross_return = float(selected[realized_col].mean())
        net_return = gross_return - cost
        nav *= 1.0 + net_return
        benchmark_return = float(group[realized_col].mean())

        rows.append(
            {
                "date": pd.to_datetime(date).strftime("%Y-%m-%d"),
                "run_id": run_id,
                "model_name": model_name,
                "model_source": model_source,
                "model_category": model_category,
                "topk": int(topk),
                "holding_days": int(holding_days),
                "rank_by": rank_by,
                "selected_codes": ",".join(codes),
                "selected_names": ",".join(names),
                "mean_pred_5d_ret": float(selected["pred_5d_ret"].mean()),
                "mean_future_5d_ret": gross_return,
                "gross_return": gross_return,
                "turnover": float(rebalance.turnover),
                "buy_turnover": float(rebalance.buy_turnover),
                "sell_turnover": float(rebalance.sell_turnover),
                "bought_codes": format_code_set(rebalance.bought_codes),
                "sold_codes": format_code_set(rebalance.sold_codes),
                "cost": cost,
                "net_return": net_return,
                "cum_return": nav - 1.0,
                "nav": nav,
                "benchmark_return": benchmark_return,
                "excess_return": net_return - benchmark_return,
            }
        )
        previous_codes = current_codes

    daily = pd.DataFrame(rows, columns=DAILY_RETURN_COLUMNS)
    if daily.empty:
        raise RuntimeError(f"no daily returns generated for {model_name} topk={topk}")
    return daily


def summarize_run(
    daily: pd.DataFrame,
    pred: pd.DataFrame,
    *,
    run_id: str,
    timestamp: str,
    model_name: str,
    model_source: str,
    model_category: str,
    has_pretrained_weight: bool,
    trained_locally: bool,
    checkpoint_path: str,
    repo_url: str,
    adapter_name: str,
    topk: int,
    holding_days: int,
    rank_by: str,
    cost_mode: str,
    target_metric: str,
    target_value: float,
    daily_returns_csv: Path,
    prediction_csv: Path,
    metrics_json: Path,
) -> dict:
    periods_per_year = 252 / max(int(holding_days), 1)
    nav_with_start = pd.concat([pd.Series([1.0]), daily["nav"]], ignore_index=True)
    realized_col = realized_return_column_for_holding(pred, holding_days)
    ic_metrics = summarize_ic(pred.dropna(subset=[realized_col]), pred_col="raw_score", target_col=realized_col)

    metrics = {
        "run_id": run_id,
        "timestamp": timestamp,
        "model_name": model_name,
        "model_source": model_source,
        "model_category": model_category,
        "has_pretrained_weight": bool(has_pretrained_weight),
        "trained_locally": bool(trained_locally),
        "checkpoint_path": checkpoint_path,
        "repo_url": repo_url,
        "commit_hash": "",
        "adapter_name": adapter_name,
        "topk": int(topk),
        "holding_days": int(holding_days),
        "rank_by": rank_by,
        "cost_mode": cost_mode,
        "start_date": daily["date"].min(),
        "end_date": daily["date"].max(),
        "num_days": int(pd.to_datetime(pred["date"]).nunique()),
        "annual_return": calc_annual_return(nav_with_start, trading_days=periods_per_year),
        "cum_return": float(daily["cum_return"].iloc[-1]),
        "final_nav": float(daily["nav"].iloc[-1]),
        "annual_volatility": calc_annual_volatility(daily["net_return"], trading_days=periods_per_year),
        "sharpe": calc_sharpe(daily["net_return"], trading_days=periods_per_year),
        "max_drawdown": calc_max_drawdown(nav_with_start),
        "win_rate": calc_win_rate(daily["net_return"]),
        "mean_daily_return": float(daily["net_return"].mean()),
        "mean_turnover": float(daily["turnover"].mean()),
        "mean_cost": float(daily["cost"].mean()),
        **ic_metrics,
        "target_metric": target_metric,
        "target_value": float(target_value),
        "daily_returns_csv": str(daily_returns_csv),
        "prediction_csv": str(prediction_csv),
        "metrics_json": str(metrics_json),
        "status": "success",
        "fail_reason": "",
        "notes": DISCLAIMER,
    }
    target = metrics.get(target_metric)
    metrics["target_hit"] = bool(target is not None and pd.notna(target) and float(target) >= float(target_value))
    return {col: metrics.get(col, "") for col in MASTER_COLUMNS}


def append_master_table(rows: list[dict]) -> None:
    new_df = pd.DataFrame(rows, columns=MASTER_COLUMNS)
    if MASTER_TABLE_PATH.exists():
        old_df = pd.read_csv(MASTER_TABLE_PATH, encoding="utf-8-sig")
        combined = pd.concat([old_df, new_df], ignore_index=True)
        combined = combined.drop_duplicates(["run_id"], keep="last")
    else:
        combined = new_df
    MASTER_TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(MASTER_TABLE_PATH, index=False, encoding="utf-8-sig")


def run_model_backtest(args: argparse.Namespace) -> list[dict]:
    ensure_dirs()
    entry = get_model_entry(args.model_name)
    meta = get_model_metadata(entry.name) or {}
    if args.auto_download and meta.get("status") != "downloaded":
        from model_zoo.downloader import download_model

        meta = download_model(entry.name)
    if meta.get("status") != "downloaded":
        raise RuntimeError(
            f"{entry.name} is not downloaded. Run: py -m model_zoo.downloader --model {entry.name}"
        )

    raw, feature = load_cached_data()
    dates = select_prediction_dates(feature, args.backtest_days, args.start_date, args.end_date)
    pred = predict_zoo_scores_for_dates(
        model_name=entry.name,
        raw_data=raw,
        feature_data=feature,
        prediction_dates=dates,
        device=args.device,
        context_length=args.context_length,
        batch_size=args.batch_size,
    )
    pred = normalize_predictions(pred, model_name=entry.name, rank_by=args.rank_by)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prediction_csv = PREDICTIONS_DIR / f"{timestamp}_{entry.name}_predictions.csv"
    pred.to_csv(prediction_csv, index=False, encoding="utf-8-sig")

    master_rows = []
    for topk in parse_topk_list(args.topk):
        run_id = f"{timestamp}_{entry.name}_top{topk}_{uuid4().hex[:8]}"
        daily = build_daily_returns(
            pred=pred,
            run_id=run_id,
            model_name=entry.name,
            model_source=entry.provider,
            model_category="A" if meta.get("status") == "downloaded" else "B",
            topk=topk,
            holding_days=args.holding_days,
            rank_by=args.rank_by,
            cost_rate=args.cost_rate,
            buy_cost=args.buy_cost,
            sell_cost=args.sell_cost,
            stamp_tax=args.stamp_tax,
        )
        daily_returns_csv = DAILY_RETURNS_DIR / f"{run_id}_daily_returns.csv"
        metrics_json = METRICS_DIR / f"{run_id}_metrics.json"
        daily.to_csv(daily_returns_csv, index=False, encoding="utf-8-sig")
        row = summarize_run(
            daily,
            pred,
            run_id=run_id,
            timestamp=timestamp,
            model_name=entry.name,
            model_source=entry.provider,
            model_category="A",
            has_pretrained_weight=True,
            trained_locally=False,
            checkpoint_path=str(meta.get("local_path") or entry.local_path),
            repo_url=entry.hf_repo,
            adapter_name=entry.adapter,
            topk=topk,
            holding_days=args.holding_days,
            rank_by=args.rank_by,
            cost_mode=(
                f"flat_single_side_cost_rate={args.cost_rate}"
                if args.cost_rate is not None
                else f"buy={args.buy_cost};sell={args.sell_cost};stamp_tax={args.stamp_tax}"
            ),
            target_metric=args.target_metric,
            target_value=args.target_value,
            daily_returns_csv=daily_returns_csv,
            prediction_csv=prediction_csv,
            metrics_json=metrics_json,
        )
        metrics_json.write_text(
            json.dumps({**row, "disclaimer": DISCLAIMER}, ensure_ascii=False, indent=2, default=_jsonable),
            encoding="utf-8",
        )
        master_rows.append(row)

    append_master_table(master_rows)
    return master_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run unified model-zoo TopK backtest.")
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--topk", default="10,30,50")
    parser.add_argument("--holding-days", type=int, default=1)
    parser.add_argument("--backtest-days", type=int, default=60)
    parser.add_argument("--rank-by", default="score")
    parser.add_argument("--cost-rate", type=float, default=None)
    parser.add_argument("--buy-cost", type=float, default=0.0003)
    parser.add_argument("--sell-cost", type=float, default=0.0003)
    parser.add_argument("--stamp-tax", type=float, default=0.0005)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--context-length", type=int, default=64)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--target-metric", default="annual_return")
    parser.add_argument("--target-value", type=float, default=0.10)
    parser.add_argument("--auto-download", action="store_true")
    return parser.parse_args()


def main() -> None:
    try:
        rows = run_model_backtest(parse_args())
    except Exception as exc:
        ensure_dirs()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        fail_row = {col: "" for col in MASTER_COLUMNS}
        fail_row.update(
            {
                "run_id": f"{timestamp}_failed_{uuid4().hex[:8]}",
                "timestamp": timestamp,
                "status": "failed",
                "fail_reason": str(exc),
                "notes": DISCLAIMER,
            }
        )
        append_master_table([fail_row])
        print(f"[Backtest Failed] {exc}")
        raise SystemExit(1)

    print(f"[Backtest] completed rows={len(rows)}")
    print(f"[Backtest] master_table={MASTER_TABLE_PATH}")
    for row in rows:
        print(
            f"[Backtest] {row['model_name']} top{row['topk']} "
            f"annual_return={row['annual_return']} target_hit={row['target_hit']}"
        )


if __name__ == "__main__":
    main()
