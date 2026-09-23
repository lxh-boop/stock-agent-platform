from __future__ import annotations

"""MyQuant/GM backtest strategy: daily 10:00 rebalance from local model targets.

Generate the input first:

    python tools/export_gm_targets.py \
      --input outputs/ranking_history.csv \
      --top-k 10 \
      --output strategies/gm_daily_1000_targets.csv

Then run this file with the MyQuant terminal / Python SDK using MODE_BACKTEST.
Set GM_TARGETS_PATH if you do not want to copy the CSV beside this strategy.
"""

import os
from pathlib import Path
import pandas as pd

# gm is provided by the MyQuant terminal/SDK environment, not by this repo.
from gm.api import *  # noqa: F401,F403


def _load_targets() -> pd.DataFrame:
    configured = os.getenv("GM_TARGETS_PATH", "").strip()
    path = Path(configured) if configured else Path(__file__).with_name("gm_daily_1000_targets.csv")
    if not path.is_file():
        raise FileNotFoundError(
            f"GM target file not found: {path}; "
            "run tools/export_gm_targets.py first or set GM_TARGETS_PATH"
        )
    df = pd.read_csv(path, encoding="utf-8-sig")
    required = {"signal_date", "symbol", "target_weight"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"GM target file missing columns: {sorted(missing)}")
    df["signal_date"] = df["signal_date"].astype(str).str[:10]
    if "execution_hint_date" in df.columns:
        df["execution_hint_date"] = df["execution_hint_date"].astype(str).str[:10]
    else:
        df["execution_hint_date"] = ""
    df["symbol"] = df["symbol"].astype(str)
    df["target_weight"] = pd.to_numeric(df["target_weight"], errors="coerce")
    return df.dropna(subset=["target_weight"])


def init(context):
    context.targets = _load_targets()
    context.dates = sorted(context.targets["signal_date"].unique().tolist())
    context.executed_dates = set()
    # date_rule=1 means every trading day in GM's scheduled task API.
    schedule(schedule_func=rebalance, date_rule=1, time_rule="10:00:00")


def _latest_signal_date(context, today: str) -> str | None:
    # Prefer an explicit execution date produced by the prediction pipeline.
    hinted = context.targets[
        (context.targets["execution_hint_date"] == today)
        & (context.targets["signal_date"] < today)
    ]
    if not hinted.empty:
        return str(hinted["signal_date"].max())

    # Otherwise use the latest signal strictly before the execution day. This
    # is the safe default and prevents look-ahead bias.
    previous = [date for date in context.dates if date < today]
    return previous[-1] if previous else None


def _safe_order_target_percent(symbol: str, percent: float):
    # Keep this wrapper tolerant to common gm SDK releases.
    try:
        return order_target_percent(
            symbol=symbol,
            percent=float(percent),
            position_side=PositionSide_Long,
            order_type=OrderType_Market,
        )
    except TypeError:
        return order_target_percent(symbol=symbol, percent=float(percent))


def _long_positions(context):
    positions = get_positions() or []
    long_side = globals().get("PositionSide_Long", 1)
    return [p for p in positions if getattr(p, "side", long_side) == long_side]


def rebalance(context):
    today = context.now.strftime("%Y-%m-%d")
    if today in context.executed_dates:
        return

    signal_date = _latest_signal_date(context, today)
    if signal_date is None:
        return

    group = context.targets[context.targets["signal_date"] == signal_date]
    targets = dict(zip(group["symbol"], group["target_weight"].astype(float)))

    # Sell removed names first to release cash.
    for position in _long_positions(context):
        symbol = str(getattr(position, "symbol", ""))
        if symbol and symbol not in targets:
            _safe_order_target_percent(symbol, 0.0)

    # Then adjust/add the target names.
    for symbol, weight in targets.items():
        if weight > 0:
            _safe_order_target_percent(symbol, weight)

    context.executed_dates.add(today)
    print(
        f"[gm_daily_1000] {today} uses signal {signal_date}; "
        f"target_count={len(targets)} weight_sum={sum(targets.values()):.4f}"
    )


if __name__ == "__main__":
    run(
        strategy_id=os.getenv("GM_STRATEGY_ID", ""),
        filename="gm_daily_1000.py",
        mode=MODE_BACKTEST,
        token=os.getenv("GM_TOKEN", ""),
        backtest_start_time=os.getenv("GM_BACKTEST_START", "2024-01-01 09:00:00"),
        backtest_end_time=os.getenv("GM_BACKTEST_END", "2026-01-01 15:00:00"),
        backtest_adjust=ADJUST_PREV,
        backtest_initial_cash=float(os.getenv("GM_INITIAL_CASH", "1000000")),
        backtest_commission_ratio=float(os.getenv("GM_COMMISSION_RATIO", "0.0003")),
        backtest_slippage_ratio=float(os.getenv("GM_SLIPPAGE_RATIO", "0.0002")),
    )
