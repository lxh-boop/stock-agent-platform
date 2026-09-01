from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from config import ACTIVE_RANKING_FEATURE_DIR


@lru_cache(maxsize=64)
def _cached_close_rows(path_text: str, modified_ns: int, trade_date: str) -> tuple[tuple[str, float], ...]:
    del modified_ns
    compact_date = str(trade_date).replace("-", "")[:8]
    matches: list[pd.DataFrame] = []
    for chunk in pd.read_csv(
        path_text,
        usecols=["ts_code", "trade_date", "close"],
        dtype={"ts_code": str, "trade_date": str},
        chunksize=100_000,
    ):
        selected = chunk[chunk["trade_date"].astype(str).str[:8].eq(compact_date)]
        if not selected.empty:
            matches.append(selected)
    if not matches:
        return ()
    frame = pd.concat(matches, ignore_index=True)
    frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0].str.zfill(6)
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["close"]).drop_duplicates("code", keep="last")
    return tuple((str(row.code), float(row.close)) for row in frame.itertuples(index=False))


def load_ranker_close_for_date(
    trade_date: str,
    *,
    feature_dir: str | Path = ACTIVE_RANKING_FEATURE_DIR,
) -> pd.DataFrame:
    path = Path(feature_dir) / "daily_basic.csv"
    if not path.is_file():
        return pd.DataFrame(columns=["code", "date", "close"])
    rows = _cached_close_rows(str(path.resolve()), path.stat().st_mtime_ns, str(trade_date))
    if not rows:
        return pd.DataFrame(columns=["code", "date", "close"])
    frame = pd.DataFrame(rows, columns=["code", "close"])
    frame["date"] = str(pd.Timestamp(trade_date).date())
    return frame.loc[:, ["code", "date", "close"]]


__all__ = ["load_ranker_close_for_date"]
