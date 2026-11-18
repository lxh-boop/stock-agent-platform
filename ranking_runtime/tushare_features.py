from __future__ import annotations

from core.user_scope import owned_path

import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from requests.exceptions import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout

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
INDEX_REQUEST_DAYS = 250


def _request_error_category(error: Exception) -> str:
    """Only fixed categories leave this module; provider messages may contain secrets."""
    text = str(error).lower()
    if any(marker in text for marker in ("token不对", "token无效", "invalid token", "token is invalid")):
        return "invalid_token"
    if any(marker in text for marker in ("每天", "每日", "每小时", "daily limit", "quota")):
        return "quota_exceeded"
    if any(marker in text for marker in ("每分钟", "频率", "频次", "rate limit", "too many requests", "429")):
        return "rate_limited"
    if any(marker in text for marker in ("权限", "积分", "permission", "forbidden")):
        return "permission_denied"
    if isinstance(error, (TimeoutError, ConnectionError, RequestsTimeout, RequestsConnectionError)) or any(
        marker in text for marker in ("timeout", "timed out", "connection", "连接", "temporar", "稍后", "502", "503", "504")
    ):
        return "temporarily_unavailable"
    return "request_failed"


def _request_index_range(pro: Any, *, code: str, start: str, end: str) -> Any:
    for attempt in range(3):
        try:
            return pro.index_daily(ts_code=code, start_date=start, end_date=end, fields=INDEX_FIELDS)
        except Exception as error:
            category = _request_error_category(error)
            if attempt == 2 or category not in {"rate_limited", "temporarily_unavailable"}:
                raise
            time.sleep(60 if category == "rate_limited" else 2 ** attempt)


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
                {"trade_date": trade_date, "error_type": type(exc).__name__,
                 "error_category": _request_error_category(exc)}
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


def _fetch_missing_index_dates(*, pro: Any, path: Path, trade_dates: list[str], report: dict[str, Any]) -> None:
    """Fetch each index by bounded date range, retaining successful ranges for retries."""
    cached = _read(path)
    pairs = set() if cached.empty else set(zip(cached["ts_code"].astype(str), cached["trade_date"].astype(str)))
    missing_dates = [day for day in trade_dates if any((code, day) not in pairs for code in INDEX_CODES)]
    errors: list[dict[str, str]] = []
    written = 0
    for code in INDEX_CODES:
        for offset in range(0, len(trade_dates), INDEX_REQUEST_DAYS):
            window = trade_dates[offset:offset + INDEX_REQUEST_DAYS]
            missing = [day for day in window if (code, day) not in pairs]
            if not missing:
                continue
            try:
                response = _request_index_range(pro, code=code, start=missing[0], end=missing[-1])
                frame = pd.DataFrame() if response is None else pd.DataFrame(response)
                if frame.empty:
                    continue
                frame["trade_date"] = frame["trade_date"].astype(str).str[:8]
                frame["ts_code"] = frame["ts_code"].astype(str)
                frame = frame.loc[frame["ts_code"].eq(code) & frame["trade_date"].isin(missing)]
            except Exception as error:
                errors.append({"ts_code": code, "start_date": missing[0], "end_date": missing[-1],
                               "error_type": type(error).__name__, "error_category": _request_error_category(error)})
                continue
            if not frame.empty:
                # Commit each successful range before requesting another; a later failure cannot discard it.
                written += _upsert(path, frame, ["ts_code", "trade_date"])
                pairs.update(zip(frame["ts_code"], frame["trade_date"]))
    unresolved = [day for day in trade_dates if any((code, day) not in pairs for code in INDEX_CODES)]
    report["endpoints"]["index_daily"] = {
        "cached": not missing_dates, "required": True, "rows": written,
        "requested_dates": len(missing_dates), "resolved_dates": len(missing_dates) - len(unresolved),
        "unresolved_dates": unresolved, "errors": errors,
    }
    if unresolved:
        report["ready"] = False


def summarize_feature_refresh_failure(report: dict[str, Any]) -> str:
    labels = {"invalid_token": "Token 无效", "permission_denied": "接口权限不足",
              "quota_exceeded": "调用额度已用尽", "rate_limited": "接口限频",
              "temporarily_unavailable": "网络或服务暂时不可用", "request_failed": "接口请求失败"}
    details = []
    for name, endpoint in report.get("endpoints", {}).items():
        missing = endpoint.get("unresolved_dates") or []
        if not endpoint.get("required") or not missing:
            continue
        reason = "、".join(sorted({labels.get(item.get("error_category"), "接口请求失败")
                                  for item in endpoint.get("errors", [])}))
        details.append(f"{name} 缺少 {len(missing)} 个交易日（{min(missing)}～{max(missing)}）；{reason or '接口返回数据不完整'}")
    return "主动排名所需 Tushare 特征未就绪：" + "；".join(details or ["请检查必需数据源的覆盖范围"])


def _open_dates_for_catchup(pro: Any, directory: Path, signal_date: str) -> list[str]:
    compact = str(signal_date).replace("-", "")[:8]
    signal = pd.to_datetime(compact, format="%Y%m%d", errors="raise")
    required_paths = [directory / "daily_basic.csv", directory / "moneyflow.csv"]
    latest_dates: list[pd.Timestamp] = []
    for path in required_paths:
        dates = _cached_dates(path)
        if dates:
            latest_dates.append(pd.Timestamp(max(dates)))
    # 每次检查模型最长 250 日特征的历史覆盖，避免重试时遗漏其他接口较早的缺口。
    recent_start = signal - timedelta(days=450)
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
    directory = owned_path(feature_dir)
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
    _fetch_missing_index_dates(
        pro=pro,
        report=report,
        path=directory / "index_daily.csv",
        trade_dates=trade_dates,
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


__all__ = ["refresh_ranker_features_for_date", "refresh_ranker_features_through", "summarize_feature_refresh_failure"]
