from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_tushare import get_token, init_tushare_pro, resolve_daily_data_end_date, to_ts_code
from core.tushare_feature_contract import RANKER_INDEX_CODES


DEFAULT_PREDICTION_HISTORY = (
    ROOT
    / "outputs"
    / "backtests"
    / "predictions"
    / "target_full_recent_v1_kronos_mini_t1_predictions.csv"
)
DEFAULT_OUTPUT_DIR = ROOT / "data" / "model_precision" / "tushare"
DEFAULT_INDEX_CODES = RANKER_INDEX_CODES

DAILY_BASIC_FIELDS = (
    "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
    "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
    "free_share,total_mv,circ_mv"
)
MONEYFLOW_FIELDS = (
    "ts_code,trade_date,buy_sm_vol,buy_sm_amount,sell_sm_vol,sell_sm_amount,"
    "buy_md_vol,buy_md_amount,sell_md_vol,sell_md_amount,buy_lg_vol,"
    "buy_lg_amount,sell_lg_vol,sell_lg_amount,buy_elg_vol,buy_elg_amount,"
    "sell_elg_vol,sell_elg_amount,net_mf_vol,net_mf_amount"
)
INDEX_DAILY_FIELDS = (
    "ts_code,trade_date,close,open,high,low,pre_close,change,pct_chg,vol,amount"
)


class RequestRateLimiter:
    def __init__(self, minimum_interval_seconds: float) -> None:
        self.minimum_interval_seconds = max(float(minimum_interval_seconds), 0.0)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self.minimum_interval_seconds
        if delay:
            time.sleep(delay)


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, path)


def _atomic_json(payload: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _merge_manifest_reports(
    manifest_path: Path,
    reports: list[dict[str, object]],
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing_start = str(existing.get("start_date") or "")
            existing_end = str(existing.get("end_date") or "")
            for item in existing.get("reports") or []:
                if not isinstance(item, dict) or not item.get("endpoint"):
                    continue
                previous = dict(item)
                previous.setdefault("start_date", existing_start)
                previous.setdefault("end_date", existing_end)
                output = Path(str(previous.get("output") or ""))
                if output.exists():
                    merged[str(previous["endpoint"])] = previous
        except Exception:
            merged = {}
    for item in reports:
        current = dict(item)
        current["start_date"] = start_date
        current["end_date"] = end_date
        merged[str(current["endpoint"])] = current
    endpoint_order = {
        "daily_basic": 0,
        "moneyflow": 1,
        "margin_detail": 2,
        "index_daily": 3,
        "moneyflow_hsgt": 4,
    }
    return sorted(
        merged.values(),
        key=lambda item: endpoint_order.get(str(item.get("endpoint") or ""), 99),
    )


def _read_codes(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"预测历史不存在：{path}")
    frame = pd.read_csv(path, dtype={"code": str}, usecols=["code"])
    codes = sorted(
        {
            str(value).split(".")[0].zfill(6)
            for value in frame["code"].dropna().astype(str)
        }
    )
    if not codes:
        raise RuntimeError("预测历史中没有股票代码")
    return codes


def _cache_complete(path: Path, *, start_date: str, end_date: str) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    metadata_path = path.with_suffix(".meta.json")
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            return bool(
                metadata.get("complete") is True
                and str(metadata.get("requested_start_date") or "") <= start_date
                and str(metadata.get("requested_end_date") or "") >= end_date
            )
        except Exception:
            pass
    try:
        dates = pd.read_csv(path, usecols=["trade_date"], dtype=str)["trade_date"]
    except Exception:
        return False
    if dates.empty:
        return False
    values = dates.astype(str).str.replace("-", "", regex=False).str[:8]
    # A newly listed or delisted security legitimately has no rows for part of
    # the requested range. Existing full-range downloads can be reused when
    # they reach the requested end; the sidecar handles delisted/empty cases.
    return bool(values.max() >= end_date)


def _thread_client(token: str, local: threading.local):
    client = getattr(local, "client", None)
    if client is None:
        client = init_tushare_pro(token)
        local.client = client
    return client


def _call_with_retry(
    call: Callable[[], pd.DataFrame],
    *,
    limiter: RequestRateLimiter,
    retries: int,
) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(max(1, int(retries))):
        try:
            limiter.wait()
            result = call()
            return pd.DataFrame() if result is None else result.copy()
        except Exception as exc:  # Tushare raises a generic Exception for API errors.
            last_error = exc
            if attempt + 1 < max(1, int(retries)):
                time.sleep(min(8.0, 1.5 * (2**attempt)))
    assert last_error is not None
    raise RuntimeError(type(last_error).__name__) from last_error


def _stock_endpoint_call(
    endpoint: str,
    *,
    client,
    ts_code: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    if endpoint == "daily_basic":
        return client.daily_basic(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields=DAILY_BASIC_FIELDS,
        )
    if endpoint == "moneyflow":
        return client.moneyflow(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields=MONEYFLOW_FIELDS,
        )
    if endpoint == "margin_detail":
        return client.margin_detail(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
        )
    raise ValueError(f"不支持的股票接口：{endpoint}")


def download_stock_endpoint(
    endpoint: str,
    *,
    codes: list[str],
    token: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    workers: int,
    minimum_interval_seconds: float,
    retries: int,
) -> dict[str, object]:
    cache_dir = output_dir / "by_stock" / endpoint
    cache_dir.mkdir(parents=True, exist_ok=True)
    pending = [
        code
        for code in codes
        if not _cache_complete(
            cache_dir / f"{code}.csv",
            start_date=start_date,
            end_date=end_date,
        )
    ]
    print(f"[{endpoint}] total={len(codes)} cached={len(codes) - len(pending)} pending={len(pending)}")

    limiter = RequestRateLimiter(minimum_interval_seconds)
    local = threading.local()
    failures: dict[str, str] = {}
    completed = 0

    def fetch(code: str) -> tuple[str, int]:
        client = _thread_client(token, local)
        ts_code = to_ts_code(code)
        frame = _call_with_retry(
            lambda: _stock_endpoint_call(
                endpoint,
                client=client,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            ),
            limiter=limiter,
            retries=retries,
        )
        if not frame.empty and "trade_date" in frame.columns:
            frame["trade_date"] = frame["trade_date"].astype(str).str[:8]
            frame = frame.sort_values("trade_date").drop_duplicates(
                ["ts_code", "trade_date"], keep="last"
            )
        _atomic_csv(frame, cache_dir / f"{code}.csv")
        _atomic_json(
            {
                "complete": True,
                "requested_start_date": start_date,
                "requested_end_date": end_date,
                "rows": int(len(frame)),
            },
            cache_dir / f"{code}.meta.json",
        )
        return code, int(len(frame))

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = {executor.submit(fetch, code): code for code in pending}
        for future in as_completed(futures):
            code = futures[future]
            try:
                future.result()
                completed += 1
            except Exception as exc:
                failures[code] = type(exc).__name__
            processed = completed + len(failures)
            if processed % 25 == 0 or processed == len(pending):
                print(
                    f"[{endpoint}] progress={processed}/{len(pending)} "
                    f"ok={completed} failed={len(failures)}"
                )

    frames: list[pd.DataFrame] = []
    for code in codes:
        path = cache_dir / f"{code}.csv"
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            frame = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str})
        except pd.errors.EmptyDataError:
            continue
        if not frame.empty:
            frames.append(frame)
    combined = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    if not combined.empty:
        combined = combined.sort_values(["trade_date", "ts_code"]).drop_duplicates(
            ["ts_code", "trade_date"], keep="last"
        )
    combined_path = output_dir / f"{endpoint}.csv"
    _atomic_csv(combined, combined_path)
    return {
        "endpoint": endpoint,
        "rows": int(len(combined)),
        "codes": int(combined["ts_code"].nunique()) if "ts_code" in combined else 0,
        "failed_codes": sorted(failures),
        "output": str(combined_path),
    }


def download_indices(
    *,
    token: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    minimum_interval_seconds: float,
    retries: int,
) -> dict[str, object]:
    client = init_tushare_pro(token)
    limiter = RequestRateLimiter(minimum_interval_seconds)
    frames = []
    for ts_code in DEFAULT_INDEX_CODES:
        frame = _call_with_retry(
            lambda ts_code=ts_code: client.index_daily(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
                fields=INDEX_DAILY_FIELDS,
            ),
            limiter=limiter,
            retries=retries,
        )
        frames.append(frame)
        print(f"[index_daily] {ts_code} rows={len(frame)}")
    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined = combined.sort_values(["trade_date", "ts_code"]).drop_duplicates(
        ["ts_code", "trade_date"], keep="last"
    )
    output = output_dir / "index_daily.csv"
    _atomic_csv(combined, output)
    return {
        "endpoint": "index_daily",
        "rows": int(len(combined)),
        "codes": int(combined["ts_code"].nunique()),
        "failed_codes": [],
        "output": str(output),
    }


def download_hsgt(
    *,
    token: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    minimum_interval_seconds: float,
    retries: int,
) -> dict[str, object]:
    client = init_tushare_pro(token)
    limiter = RequestRateLimiter(minimum_interval_seconds)
    frames = []
    for year in range(int(start_date[:4]), int(end_date[:4]) + 1):
        chunk_start = max(start_date, f"{year}0101")
        chunk_end = min(end_date, f"{year}1231")
        frame = _call_with_retry(
            lambda chunk_start=chunk_start, chunk_end=chunk_end: client.moneyflow_hsgt(
                start_date=chunk_start,
                end_date=chunk_end,
            ),
            limiter=limiter,
            retries=retries,
        )
        frames.append(frame)
        print(f"[moneyflow_hsgt] {year} rows={len(frame)}")
    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined = combined.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    output = output_dir / "moneyflow_hsgt.csv"
    _atomic_csv(combined, output)
    return {
        "endpoint": "moneyflow_hsgt",
        "rows": int(len(combined)),
        "failed_codes": [],
        "output": str(output),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="下载用于T+1涨跌分类头的Tushare历史特征，不输出或持久化Token。"
    )
    parser.add_argument("--start-date", default="20180101")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--prediction-history", type=Path, default=DEFAULT_PREDICTION_HISTORY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--endpoints",
        default="daily_basic,moneyflow,index_daily,moneyflow_hsgt",
        help="逗号分隔：daily_basic,moneyflow,margin_detail,index_daily,moneyflow_hsgt",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--minimum-interval-seconds", type=float, default=0.16)
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = get_token()
    client = init_tushare_pro(token)
    end_date = str(args.end_date or "").replace("-", "")
    if not end_date:
        end_date, _ = resolve_daily_data_end_date(client)
    start_date = str(args.start_date).replace("-", "")
    if len(start_date) != 8 or len(end_date) != 8 or start_date > end_date:
        raise ValueError("start_date/end_date 必须是有效的YYYYMMDD范围")

    codes = _read_codes(Path(args.prediction_history))
    endpoints = [value.strip() for value in str(args.endpoints).split(",") if value.strip()]
    supported = {
        "daily_basic",
        "moneyflow",
        "margin_detail",
        "index_daily",
        "moneyflow_hsgt",
    }
    unknown = sorted(set(endpoints) - supported)
    if unknown:
        raise ValueError(f"不支持的接口：{unknown}")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"[Tushare Precision Features] codes={len(codes)} "
        f"range={start_date}..{end_date} endpoints={endpoints}"
    )
    reports = []
    for endpoint in endpoints:
        if endpoint in {"daily_basic", "moneyflow", "margin_detail"}:
            reports.append(
                download_stock_endpoint(
                    endpoint,
                    codes=codes,
                    token=token,
                    output_dir=output_dir,
                    start_date=start_date,
                    end_date=end_date,
                    workers=args.workers,
                    minimum_interval_seconds=args.minimum_interval_seconds,
                    retries=args.retries,
                )
            )
        elif endpoint == "index_daily":
            reports.append(
                download_indices(
                    token=token,
                    output_dir=output_dir,
                    start_date=start_date,
                    end_date=end_date,
                    minimum_interval_seconds=args.minimum_interval_seconds,
                    retries=args.retries,
                )
            )
        elif endpoint == "moneyflow_hsgt":
            reports.append(
                download_hsgt(
                    token=token,
                    output_dir=output_dir,
                    start_date=start_date,
                    end_date=end_date,
                    minimum_interval_seconds=args.minimum_interval_seconds,
                    retries=args.retries,
                )
            )

    manifest_path = output_dir / "manifest.json"
    manifest_reports = _merge_manifest_reports(
        manifest_path,
        reports,
        start_date=start_date,
        end_date=end_date,
    )
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "start_date": start_date,
        "end_date": end_date,
        "stock_count": len(codes),
        "credential_source": "local_config_or_environment",
        "reports": manifest_reports,
    }
    _atomic_json(manifest, manifest_path)
    print("[Tushare Precision Features] complete")
    for report in reports:
        print(
            f"  {report['endpoint']}: rows={report['rows']} "
            f"failed={len(report.get('failed_codes', []))}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
