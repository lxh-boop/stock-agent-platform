from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import OUTPUT_DIR, PAPER_MARKET_OHLC_CACHE_PATH
from data_tushare import get_token, init_tushare_pro
from portfolio.paper_market_data import load_paper_ohlc_for_date, merge_paper_market_ohlc


FIELDS = "ts_code,trade_date,open,high,low,close"


def _requests(output_dir: Path, user_id: str) -> dict[str, set[str]]:
    directory = output_dir / "portfolio" / user_id / "history" / "orders"
    requests: dict[str, set[str]] = defaultdict(set)
    for path in sorted(directory.glob("orders_????????.csv")):
        trade_date = path.stem.removeprefix("orders_")
        if len(trade_date) != 8 or not trade_date.isdigit():
            continue
        try:
            frame = pd.read_csv(path, dtype={"stock_code": str})
        except (OSError, ValueError, pd.errors.ParserError):
            continue
        if frame.empty or "stock_code" not in frame.columns:
            continue
        codes = (
            frame["stock_code"]
            .astype(str)
            .str.extract(r"(\d{6})", expand=False)
            .dropna()
            .str.zfill(6)
        )
        requests[trade_date].update(codes.tolist())
    return requests


def backfill(
    *,
    user_id: str,
    output_dir: Path,
    cache_path: Path,
    start_date: str = "",
    end_date: str = "",
    request_delay_seconds: float = 0.08,
) -> dict[str, int | str]:
    requests = _requests(output_dir, user_id)
    if start_date:
        requests = {date: codes for date, codes in requests.items() if date >= start_date.replace("-", "")}
    if end_date:
        requests = {date: codes for date, codes in requests.items() if date <= end_date.replace("-", "")}
    client = None
    fetched_frames: list[pd.DataFrame] = []
    requested_pairs = fetched_pairs = missing_pairs = 0

    for index, (trade_date, codes) in enumerate(sorted(requests.items()), start=1):
        cached = load_paper_ohlc_for_date(trade_date, cache_path=cache_path)
        cached_codes = set(cached["code"].astype(str)) if not cached.empty else set()
        missing_codes = set(codes) - cached_codes
        requested_pairs += len(missing_codes)
        if not missing_codes:
            continue
        if client is None:
            client = init_tushare_pro(get_token())
        daily = client.daily(trade_date=trade_date, fields=FIELDS)
        daily = pd.DataFrame() if daily is None else pd.DataFrame(daily)
        selected = pd.DataFrame()
        if not daily.empty:
            normalized_codes = daily["ts_code"].astype(str).str.extract(r"(\d{6})", expand=False)
            selected = daily[normalized_codes.isin(missing_codes)].copy()
            if not selected.empty:
                selected["source"] = "tushare_daily"
                fetched_frames.append(selected)
                fetched_pairs += len(selected)
        missing_pairs += max(0, len(missing_codes) - len(selected))
        print(f"[{index}/{len(requests)}] {trade_date}: required={len(codes)} fetched={len(selected)}")
        if request_delay_seconds > 0:
            time.sleep(request_delay_seconds)

    merge_report = merge_paper_market_ohlc(
        pd.concat(fetched_frames, ignore_index=True, sort=False) if fetched_frames else pd.DataFrame(),
        source="tushare_daily",
        cache_path=cache_path,
    )
    return {
        "user_id": user_id,
        "dates": len(requests),
        "requested_pairs": requested_pairs,
        "fetched_pairs": fetched_pairs,
        "missing_pairs": missing_pairs,
        "cache_rows": int(merge_report["rows_after"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="补齐模拟盘历史操作所需的真实日线 OHLC；不会输出或保存 Token。")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(OUTPUT_DIR))
    parser.add_argument("--cache-path", type=Path, default=Path(PAPER_MARKET_OHLC_CACHE_PATH))
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--request-delay-seconds", type=float, default=0.08)
    args = parser.parse_args()
    print(backfill(**vars(args)))


if __name__ == "__main__":
    main()
