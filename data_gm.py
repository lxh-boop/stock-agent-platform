from __future__ import annotations

"""MyQuant/GM daily-bar adapter.

The GM SDK is intentionally imported lazily so the rest of the project, unit
tests and slim API images do not require `gm`.
"""

from pathlib import Path
import time
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd


GM_NUMERIC_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)


def _load_gm_api():
    try:
        import gm.api  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional SDK
        raise RuntimeError(
            "gm_sdk_not_installed; install the MyQuant-compatible gm SDK "
            "in the Python environment used for data fetching"
        ) from exc
    return gm.api


def set_gm_token(token: str | None) -> None:
    value = str(token or "").strip()
    if not value:
        raise RuntimeError("GM_TOKEN_is_required")
    api = _load_gm_api()
    api.set_token(value)


def normalize_gm_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        market, code = text.split(".", 1)
        if market in {"SHSE", "SZSE", "BJSE"} and code.isdigit():
            return f"{market}.{code.zfill(6)}"
        return text
    code = text.split(".")[-1].zfill(6)
    if not code.isdigit():
        return ""
    if code.startswith(("6", "9")):
        return f"SHSE.{code}"
    if code.startswith(("0", "2", "3", "7")):
        return f"SZSE.{code}"
    if code.startswith(("4", "8")):
        return f"BJSE.{code}"
    return ""


def load_symbols_file(path: str | Path) -> list[dict[str, str]]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"symbols_file_not_found:{source}")
    frame = pd.read_csv(source, dtype=str)
    rows: list[dict[str, str]] = []
    for _, row in frame.iterrows():
        code = str(row.get("code") or row.get("stock_code") or "").split(".")[-1]
        code = code.zfill(6) if code.isdigit() else ""
        symbol = normalize_gm_symbol(row.get("symbol") or row.get("ts_code") or code)
        if not code or not symbol:
            continue
        rows.append(
            {
                "code": code,
                "symbol": symbol,
                "name": str(row.get("name") or row.get("stock_name") or code),
            }
        )
    unique = {row["code"]: row for row in rows}
    return list(unique.values())


def _parse_bar_date(value: Any) -> Any:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    return parsed


def normalize_gm_bars(raw: Any, *, symbol: Any) -> pd.DataFrame:
    if raw is None:
        return pd.DataFrame()
    frame = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(list(raw))
    if frame.empty:
        return pd.DataFrame()

    normalized_symbol = normalize_gm_symbol(symbol)
    if not normalized_symbol:
        raise ValueError(f"invalid_gm_symbol:{symbol}")

    date_source = next(
        (column for column in ("eob", "bob", "trade_date", "date", "time") if column in frame.columns),
        None,
    )
    if date_source is None:
        raise ValueError(f"gm_bars_missing_date_column:{normalized_symbol}")

    out = pd.DataFrame(index=frame.index)
    out["date"] = _parse_bar_date(frame[date_source]).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    out["symbol"] = normalized_symbol
    out["code"] = normalized_symbol.split(".")[-1]
    for column in GM_NUMERIC_COLUMNS:
        out[column] = pd.to_numeric(frame.get(column), errors="coerce")
    if "turnover_rate" in frame.columns:
        out["turnover_rate"] = pd.to_numeric(frame["turnover_rate"], errors="coerce")
    elif "turnover" in frame.columns:
        out["turnover"] = pd.to_numeric(frame["turnover"], errors="coerce")

    out = out.dropna(subset=["date", "close"]).sort_values(["date"]).drop_duplicates("date", keep="last")
    return out.reset_index(drop=True)


def fetch_gm_daily_history(
    *,
    symbol: str,
    end_time: str,
    count: int = 260,
    adjust: Any | None = None,
) -> pd.DataFrame:
    api = _load_gm_api()
    gm_symbol = normalize_gm_symbol(symbol)
    if not gm_symbol:
        raise ValueError(f"invalid_gm_symbol:{symbol}")
    if count < 2:
        raise ValueError("history_count_must_be_at_least_2")

    kwargs: dict[str, Any] = {
        "symbol": gm_symbol,
        "frequency": "1d",
        "count": int(count),
        "end_time": end_time,
        "fields": "bob,eob,open,high,low,close,volume,amount",
        "df": True,
    }
    if adjust is not None:
        kwargs["adjust"] = adjust
    else:
        kwargs["adjust"] = getattr(api, "ADJUST_PREV", 1)
    raw = api.history_n(**kwargs)
    return normalize_gm_bars(raw, symbol=gm_symbol)


def fetch_gm_universe_daily_history(
    symbols: Sequence[dict[str, str] | str],
    *,
    end_time: str,
    count: int = 260,
    adjust: Any | None = None,
    request_interval_seconds: float = 0.0,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    if not symbols:
        raise ValueError("empty_gm_symbol_universe")

    frames: list[pd.DataFrame] = []
    errors: list[dict[str, str]] = []
    for index, item in enumerate(symbols):
        if isinstance(item, str):
            row = {"symbol": normalize_gm_symbol(item), "code": normalize_gm_symbol(item).split(".")[-1], "name": ""}
        else:
            symbol = normalize_gm_symbol(item.get("symbol") or item.get("code"))
            row = {
                "symbol": symbol,
                "code": symbol.split(".")[-1],
                "name": str(item.get("name") or ""),
            }
        if not row["symbol"]:
            errors.append({"symbol": str(item), "error": "invalid_symbol"})
            continue
        try:
            frame = fetch_gm_daily_history(
                symbol=row["symbol"],
                end_time=end_time,
                count=count,
                adjust=adjust,
            )
            if not frame.empty:
                frame["name"] = row["name"] or row["code"]
                frames.append(frame)
        except Exception as exc:
            errors.append({"symbol": row["symbol"], "error": f"{type(exc).__name__}: {exc}"})
        if request_interval_seconds > 0 and index + 1 < len(symbols):
            time.sleep(float(request_interval_seconds))

    if not frames:
        detail = "; ".join(f"{item['symbol']}={item['error']}" for item in errors[:5])
        raise RuntimeError(f"gm_history_empty:{detail}")
    return pd.concat(frames, ignore_index=True, sort=False), errors


def select_signal_date(
    bars: pd.DataFrame,
    *,
    expected_date: str | None = None,
    min_coverage: float = 0.90,
) -> str:
    if bars.empty or "date" not in bars:
        raise ValueError("empty_gm_bars")
    if not 0 < float(min_coverage) <= 1:
        raise ValueError("min_coverage_must_be_between_0_and_1")

    universe_size = int(bars["code"].nunique())
    minimum_rows = max(2, int(np.ceil(universe_size * float(min_coverage))))
    available = sorted(bars["date"].dropna().unique(), reverse=True)
    if expected_date:
        available = [date for date in available if str(date) <= str(expected_date)]
        if not available:
            raise RuntimeError(f"gm_signal_date_not_yet_available:{expected_date}")

    for date in available:
        count = int(bars.loc[bars["date"] == date, "code"].nunique())
        if count >= minimum_rows:
            if expected_date and str(date) != str(expected_date):
                raise RuntimeError(
                    f"gm_expected_signal_date_incomplete:expected={expected_date},"
                    f"available={date},coverage={count}/{universe_size}"
                )
            return str(date)
    raise RuntimeError(
        f"gm_signal_coverage_too_low:minimum_rows={minimum_rows},universe={universe_size}"
    )


GM_DAILY_FEATURE_COLUMNS = [
    "close_log",
    "ret_1",
    "ret_5",
    "ret_10",
    "ret_20",
    "ret_60",
    "close_to_ma_5",
    "close_to_ma_20",
    "close_to_ma_60",
    "volatility_5",
    "volatility_20",
    "volatility_60",
    "drawdown_20",
    "volume_ratio_5_20",
    "amount_ratio_5_20",
    "amount_log",
]


def build_gm_daily_feature_history(bars: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "code", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"gm_bars_missing_columns:{sorted(missing)}")

    frame = bars.copy()
    frame["date"] = frame["date"].astype(str)
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    if "symbol" not in frame:
        frame["symbol"] = frame["code"].map(
            lambda code: (
                f"SHSE.{code}" if code.startswith(("6", "9"))
                else f"BJSE.{code}" if code.startswith(("4", "8"))
                else f"SZSE.{code}"
            )
        )
    if "name" not in frame:
        frame["name"] = frame["code"]
    for column in ("close", "volume", "amount"):
        if column not in frame:
            frame[column] = np.nan
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = frame.sort_values(["code", "date"], kind="stable").drop_duplicates(
        ["code", "date"], keep="last"
    )
    grouped = frame.groupby("code", sort=False)
    frame["close_log"] = np.log1p(frame["close"].clip(lower=0.0))
    frame["amount_log"] = np.log1p(frame["amount"].clip(lower=0.0))
    frame["ret_1"] = grouped["close"].pct_change(fill_method=None)
    for horizon in (5, 10, 20, 60):
        frame[f"ret_{horizon}"] = frame["close"] / grouped["close"].shift(horizon) - 1.0

    for window in (5, 20, 60):
        rolling = grouped["close"].rolling(window, min_periods=max(3, window // 2))
        frame[f"close_to_ma_{window}"] = (
            frame["close"] / rolling.mean().reset_index(level=0, drop=True) - 1.0
        )
        returns = grouped["ret_1"].rolling(window, min_periods=max(3, window // 2))
        frame[f"volatility_{window}"] = returns.std().reset_index(level=0, drop=True)

    rolling_max_20 = grouped["close"].rolling(20, min_periods=10).max()
    frame["drawdown_20"] = frame["close"] / rolling_max_20.reset_index(level=0, drop=True) - 1.0

    for base, window, denominator in (
        ("volume_ratio_5_20", 5, 20),
        ("amount_ratio_5_20", 5, 20),
    ):
        source = "volume" if base.startswith("volume") else "amount"
        short = grouped[source].rolling(window, min_periods=3).mean().reset_index(level=0, drop=True)
        long = grouped[source].rolling(denominator, min_periods=denominator // 2).mean().reset_index(level=0, drop=True)
        frame[base] = short / long.replace(0.0, np.nan)

    frame = frame.replace([np.inf, -np.inf], np.nan)
    return frame.loc[:, ["date", "code", "symbol", "name", *GM_DAILY_FEATURE_COLUMNS]]


def build_gm_daily_feature_panel(
    bars: pd.DataFrame,
    *,
    signal_date: str,
) -> pd.DataFrame:
    frame = build_gm_daily_feature_history(bars)
    snapshot = frame[frame["date"] == str(signal_date)].copy()
    if snapshot.empty:
        raise RuntimeError(f"gm_feature_snapshot_empty:{signal_date}")
    snapshot = snapshot.dropna(subset=["ret_1"]).reset_index(drop=True)
    return snapshot.loc[:, ["date", "code", "symbol", "name", *GM_DAILY_FEATURE_COLUMNS]]
