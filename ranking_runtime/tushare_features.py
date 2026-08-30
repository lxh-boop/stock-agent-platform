from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from config import ACTIVE_RANKING_FEATURE_DIR
from data_tushare import init_tushare_pro, to_ts_code


INDEX_CODES = (
    "000001.SH",
    "399001.SZ",
    "399006.SZ",
    "000300.SH",
    "000905.SH",
    "000852.SH",
)
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
MARGIN_FIELDS = "trade_date,ts_code,rzye,rqye,rzmre,rqyl,rzche,rqchl,rqmcl,rzrqye"
INDEX_FIELDS = "ts_code,trade_date,close,open,high,low,pre_close,change,pct_chg,vol,amount"


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    os.replace(temporary, path)


def _read(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"ts_code": str, "trade_date": str})


def _has_date(path: Path, trade_date: str) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        dates = pd.read_csv(path, usecols=["trade_date"], dtype=str)["trade_date"]
    except Exception:
        return False
    return bool(dates.astype(str).str[:8].eq(trade_date).any())


def _upsert(path: Path, current: pd.DataFrame, keys: list[str]) -> int:
    existing = _read(path)
    combined = pd.concat([existing, current], ignore_index=True, sort=False)
    if combined.empty:
        _atomic_csv(combined, path)
        return 0
    combined["trade_date"] = combined["trade_date"].astype(str).str[:8]
    combined = combined.sort_values(
        ["trade_date", *[key for key in keys if key != "trade_date"]]
    ).drop_duplicates(keys, keep="last")
    _atomic_csv(combined, path)
    return int(len(current))


def _model_universe(feature_dir: Path, current_codes: set[str]) -> set[str]:
    path = feature_dir / "daily_basic.csv"
    if not path.exists():
        return {to_ts_code(code) for code in current_codes}
    try:
        historical = set(
            pd.read_csv(path, usecols=["ts_code"], dtype=str)["ts_code"]
            .dropna()
            .astype(str)
        )
    except Exception:
        historical = set()
    return historical | {to_ts_code(code) for code in current_codes}


def _cached_dates(path: Path) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    try:
        values = pd.read_csv(path, usecols=["trade_date"], dtype=str)["trade_date"]
    except Exception:
        return set()
    return set(values.dropna().astype(str).str[:8])


def _fetch_missing_dates(
    *,
    report: dict[str, Any],
    name: str,
    path: Path,
    trade_dates: list[str],
    fetch,
    keys: list[str],
    required: bool,
) -> None:
    cached = _cached_dates(path)
    missing = [trade_date for trade_date in trade_dates if trade_date not in cached]
    if not missing:
        report["endpoints"][name] = {
            "cached": True,
            "required": required,
            "requested_dates": 0,
            "unresolved_dates": [],
        }
        return
    frames: list[pd.DataFrame] = []
    errors: list[dict[str, str]] = []
    returned_dates: set[str] = set()
    for trade_date in missing:
        try:
            frame = fetch(trade_date)
            frame = pd.DataFrame() if frame is None else pd.DataFrame(frame)
            if frame.empty:
                continue
            frames.append(frame)
            if "trade_date" in frame:
                returned_dates.update(
                    frame["trade_date"].dropna().astype(str).str[:8]
                )
        except Exception as exc:
            errors.append(
                {"trade_date": trade_date, "error_type": type(exc).__name__}
            )
    combined = (
        pd.concat(frames, ignore_index=True, sort=False)
        if frames
        else pd.DataFrame()
    )
    written = _upsert(path, combined, keys) if not combined.empty else 0
    unresolved = [date for date in missing if date not in returned_dates]
    report["endpoints"][name] = {
        "cached": False,
        "rows": written,
        "required": required,
        "requested_dates": len(missing),
        "resolved_dates": len(missing) - len(unresolved),
        "unresolved_dates": unresolved,
        "errors": errors,
    }
    if required and unresolved:
        report["ready"] = False


def _open_dates_for_catchup(pro: Any, directory: Path, signal_date: str) -> list[str]:
    compact = str(signal_date).replace("-", "")[:8]
    signal = pd.to_datetime(compact, format="%Y%m%d", errors="raise")
    required_paths = [directory / "daily_basic.csv", directory / "moneyflow.csv"]
    latest_dates: list[pd.Timestamp] = []
    for path in required_paths:
        dates = _cached_dates(path)
        if dates:
            latest_dates.append(pd.Timestamp(max(dates)))
    recent_start = signal - timedelta(days=90)
    catchup_start = min(latest_dates) + timedelta(days=1) if latest_dates else signal
    start = min(recent_start, catchup_start)
    calendar = pro.trade_cal(
        exchange="SSE",
        start_date=start.strftime("%Y%m%d"),
        end_date=signal.strftime("%Y%m%d"),
        is_open="1",
        fields="cal_date,is_open",
    )
    if calendar is None or calendar.empty:
        raise RuntimeError("无法从 Tushare trade_cal 获取截面特征补齐日期")
    dates = sorted(
        set(
            calendar.loc[
                calendar["is_open"].astype(str).eq("1"), "cal_date"
            ].astype(str).str[:8]
        )
    )
    expected = signal.strftime("%Y%m%d")
    if expected not in dates:
        raise RuntimeError(f"信号日期不是交易日：{signal_date}")
    return dates


def refresh_ranker_features_through(
    *,
    token: str,
    signal_date: str,
    stock_codes: set[str] | list[str],
    feature_dir: str | Path = ACTIVE_RANKING_FEATURE_DIR,
) -> dict[str, Any]:
    trade_date = str(signal_date).replace("-", "")[:8]
    if len(trade_date) != 8:
        raise ValueError("signal_date必须是YYYY-MM-DD或YYYYMMDD")
    directory = Path(feature_dir)
    directory.mkdir(parents=True, exist_ok=True)
    pro = init_tushare_pro(token)
    trade_dates = _open_dates_for_catchup(pro, directory, trade_date)
    universe = _model_universe(
        directory,
        {str(code).split(".")[0].zfill(6) for code in stock_codes},
    )
    report: dict[str, Any] = {
        "ready": True,
        "trade_date": trade_date,
        "catchup_start_date": trade_dates[0],
        "catchup_trade_days": len(trade_dates),
        "universe_size": len(universe),
        "endpoints": {},
    }

    def filtered(frame: Any) -> pd.DataFrame:
        value = pd.DataFrame() if frame is None else pd.DataFrame(frame)
        if not value.empty and "ts_code" in value.columns:
            value = value[value["ts_code"].astype(str).isin(universe)]
        return value

    _fetch_missing_dates(
        report=report,
        name="daily_basic",
        path=directory / "daily_basic.csv",
        trade_dates=trade_dates,
        fetch=lambda date: filtered(
            pro.daily_basic(trade_date=date, fields=DAILY_BASIC_FIELDS)
        ),
        keys=["ts_code", "trade_date"],
        required=True,
    )
    _fetch_missing_dates(
        report=report,
        name="moneyflow",
        path=directory / "moneyflow.csv",
        trade_dates=trade_dates,
        fetch=lambda date: filtered(
            pro.moneyflow(trade_date=date, fields=MONEYFLOW_FIELDS)
        ),
        keys=["ts_code", "trade_date"],
        required=True,
    )
    def fetch_indexes(date: str) -> pd.DataFrame:
        frames = [
            frame
            for frame in (
                pro.index_daily(
                    ts_code=code,
                    trade_date=date,
                    fields=INDEX_FIELDS,
                )
                for code in INDEX_CODES
            )
            if frame is not None and not frame.empty
        ]
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    _fetch_missing_dates(
        report=report,
        name="index_daily",
        path=directory / "index_daily.csv",
        trade_dates=trade_dates,
        fetch=fetch_indexes,
        keys=["ts_code", "trade_date"],
        required=True,
    )
    _fetch_missing_dates(
        report=report,
        name="margin_detail",
        path=directory / "margin_detail.csv",
        trade_dates=trade_dates,
        fetch=lambda date: filtered(
            pro.margin_detail(trade_date=date, fields=MARGIN_FIELDS)
        ),
        keys=["ts_code", "trade_date"],
        required=False,
    )
    _fetch_missing_dates(
        report=report,
        name="moneyflow_hsgt",
        path=directory / "moneyflow_hsgt.csv",
        trade_dates=trade_dates,
        fetch=lambda date: pro.moneyflow_hsgt(trade_date=date),
        keys=["trade_date"],
        required=False,
    )
    return report


def refresh_ranker_features_for_date(**kwargs) -> dict[str, Any]:
    """Compatibility wrapper; catch-up is always performed to protect rolling features."""

    return refresh_ranker_features_through(**kwargs)


__all__ = ["refresh_ranker_features_for_date", "refresh_ranker_features_through"]
