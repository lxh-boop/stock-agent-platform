from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Any

import pandas as pd

from config import PAPER_MARKET_OHLC_CACHE_PATH


OHLC_COLUMNS = ["code", "date", "open", "high", "low", "close", "source"]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLC_COLUMNS)


def _normalized_frame(value: Any, *, source: str) -> pd.DataFrame:
    frame = pd.DataFrame(value).copy()
    if frame.empty:
        return _empty()
    code_column = next(
        (column for column in ("code", "stock_code", "ts_code") if column in frame.columns),
        None,
    )
    date_column = next(
        (column for column in ("date", "trade_date") if column in frame.columns),
        None,
    )
    if code_column is None or date_column is None:
        return _empty()
    required = {"open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        return _empty()

    normalized = pd.DataFrame(index=frame.index)
    normalized["code"] = (
        frame[code_column]
        .astype(str)
        .str.extract(r"(\d{6})", expand=False)
        .fillna("")
        .str.zfill(6)
    )
    date_text = frame[date_column].astype(str).str.replace(r"[^0-9]", "", regex=True).str[:8]
    normalized["date"] = pd.to_datetime(date_text, format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    for column in ("open", "high", "low", "close"):
        normalized[column] = pd.to_numeric(frame[column], errors="coerce")
    if "source" in frame.columns:
        normalized["source"] = frame["source"].fillna("").astype(str)
        normalized.loc[normalized["source"].str.strip().eq(""), "source"] = source
    else:
        normalized["source"] = source
    normalized = normalized.dropna(subset=["date", "open", "high", "low", "close"])
    normalized = normalized[
        normalized["code"].str.fullmatch(r"\d{6}")
        & normalized["code"].ne("000000")
    ]
    return normalized.loc[:, OHLC_COLUMNS].drop_duplicates(["date", "code"], keep="last")


@lru_cache(maxsize=1)
def _read_cache(path_text: str, modified_ns: int) -> pd.DataFrame:
    del modified_ns
    path = Path(path_text)
    if not path.is_file():
        return _empty()
    try:
        return _normalized_frame(pd.read_csv(path, dtype={"code": str, "date": str}), source="paper_market_cache")
    except (OSError, ValueError, pd.errors.ParserError):
        return _empty()


def load_paper_ohlc_for_date(
    trade_date: str,
    *,
    cache_path: str | Path = PAPER_MARKET_OHLC_CACHE_PATH,
) -> pd.DataFrame:
    path = Path(cache_path)
    if not path.is_file():
        return _empty()
    frame = _read_cache(str(path.resolve()), path.stat().st_mtime_ns)
    selected_date = str(pd.Timestamp(trade_date).date())
    return frame[frame["date"].eq(selected_date)].copy().reset_index(drop=True)


def merge_paper_market_ohlc(
    value: Any,
    *,
    source: str,
    cache_path: str | Path = PAPER_MARKET_OHLC_CACHE_PATH,
) -> dict[str, Any]:
    incoming = _normalized_frame(value, source=source)
    path = Path(cache_path)
    existing = (
        _normalized_frame(pd.read_csv(path, dtype={"code": str, "date": str}), source="paper_market_cache")
        if path.is_file()
        else _empty()
    )
    before = len(existing)
    combined = pd.concat([existing, incoming], ignore_index=True, sort=False)
    if not combined.empty:
        combined = (
            combined.drop_duplicates(["date", "code"], keep="last")
            .sort_values(["date", "code"], kind="stable")
            .reset_index(drop=True)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    combined.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, path)
    _read_cache.cache_clear()
    return {
        "cache_path": str(path),
        "input_rows": int(len(incoming)),
        "rows_before": int(before),
        "rows_after": int(len(combined)),
        "dates": int(combined["date"].nunique()) if not combined.empty else 0,
    }


__all__ = ["load_paper_ohlc_for_date", "merge_paper_market_ohlc"]
