import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as datetime_time, timedelta

import numpy as np
import pandas as pd

from core.config.secrets import resolve_secret

from config import EPS, START_DATE

from universe import get_stock_pool


A_SHARE_DAILY_DATA_READY_TIME = datetime_time(hour=15, minute=30)


def _tushare_module():
    try:
        import tushare as tushare_module
    except ImportError as exc:
        raise RuntimeError("当前环境缺少 tushare 包，请先安装项目依赖。") from exc
    return tushare_module


def get_token(token: str | None = None) -> str:
    try:
        from local_config import load_local_config

        saved_token = str(load_local_config().get("tushare_token") or "").strip()
    except Exception:
        saved_token = ""

    resolved = resolve_secret(
        "tushare_token",
        explicit=token,
        local_value=saved_token,
        env_names=("TUSHARE_TOKEN",),
        file_env_names=("STOCK_TUSHARE_TOKEN_FILE",),
        prefer_external=False,
    )
    if resolved.value:
        return resolved.value

    raise RuntimeError(
        "没有找到 Tushare Token。请在系统设置中填写，或配置 TUSHARE_TOKEN / STOCK_TUSHARE_TOKEN_FILE。"
    )


def init_tushare(token: str | None = None):
    token = get_token(token)
    _tushare_module().set_token(token)
    return token


def init_tushare_pro(token: str | None = None):
    token = init_tushare(token)
    return _tushare_module().pro_api(token)


def validate_tushare_token(token: str) -> tuple[bool, str]:
    """
    验证 Tushare token 是否可用。
    """
    try:
        pro = init_tushare_pro(token)

        df = pro.daily(
            ts_code="000001.SZ",
            start_date="20240101",
            end_date="20240110",
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
        )

        if df is None or df.empty:
            return False, "连接成功，但没有返回数据。可能是 token 权限不足或接口限制。"

        return True, f"连接成功，测试数据行数：{len(df)}"

    except Exception as e:
        return False, f"连接失败：{e}"


def _format_trade_date_text(value: str) -> str:
    text = str(value or "").replace("-", "")[:8]
    if len(text) != 8:
        return str(value or "")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def build_market_data_window(
    pro,
    now: datetime | None = None,
    exchange: str = "SSE",
) -> dict:
    now = now or datetime.now()
    today = now.strftime("%Y%m%d")
    start_date = (now - timedelta(days=60)).strftime("%Y%m%d")
    end_date = (now + timedelta(days=60)).strftime("%Y%m%d")
    cal = pro.trade_cal(
        exchange=exchange,
        start_date=start_date,
        end_date=end_date,
        is_open="1",
        fields="cal_date,is_open",
    )
    if cal is None or cal.empty:
        raise RuntimeError("无法从 Tushare trade_cal 获取交易日历。")

    trade_dates = (
        cal[cal["is_open"].astype(str) == "1"]["cal_date"]
        .astype(str)
        .sort_values()
        .tolist()
    )
    if not trade_dates:
        raise RuntimeError("Tushare trade_cal 未返回开市交易日。")

    is_trade_day = today in trade_dates
    previous_dates = [date for date in trade_dates if date < today]
    next_dates = [date for date in trade_dates if date > today]

    if is_trade_day and now.time() >= A_SHARE_DAILY_DATA_READY_TIME:
        expected_signal_date = today
        data_status = "after_close_expect_today"
        target_candidates = [date for date in trade_dates if date > expected_signal_date]
    elif is_trade_day:
        if not previous_dates:
            raise RuntimeError("交易日历中没有上一交易日。")
        expected_signal_date = previous_dates[-1]
        data_status = "before_close_or_data_ready"
        target_candidates = [today]
    else:
        if not previous_dates:
            raise RuntimeError("交易日历中没有最近已完成交易日。")
        expected_signal_date = previous_dates[-1]
        data_status = "non_trading_day"
        target_candidates = next_dates

    if not target_candidates:
        target_candidates = [date for date in trade_dates if date > expected_signal_date]
    if not target_candidates:
        raise RuntimeError("交易日历中没有下一交易日。")

    return {
        "data_status": data_status,
        "expected_signal_date": _format_trade_date_text(expected_signal_date),
        "prediction_target_date": _format_trade_date_text(target_candidates[0]),
        "ready_time": A_SHARE_DAILY_DATA_READY_TIME.strftime("%H:%M"),
    }


def format_code(code) -> str:
    return str(code).zfill(6)


def to_ts_code(code: str) -> str:
    code = format_code(code)

    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"

    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"

    if code.startswith(("4", "8")):
        return f"{code}.BJ"

    raise ValueError(f"无法识别交易所的股票代码：{code}")


def ts_code_to_code(ts_code: str) -> str:
    return str(ts_code).split(".")[0].zfill(6)


def resolve_daily_data_end_date(
    pro,
    end_date: str | None = None,
    now: datetime | None = None,
    exchange: str = "SSE",
) -> tuple[str, str]:
    """
    解析每日更新应使用的行情截止日。

    A 股日线信号只在收盘后才应使用当日数据；未到收盘后数据可用时间时，
    默认回退到最近一个已完成交易日，避免盘中数据污染“下一交易日”预测。
    """
    if end_date:
        return str(end_date).replace("-", ""), "使用用户指定的行情截止日。"

    now = now or datetime.now()
    today_text = now.strftime("%Y%m%d")
    cutoff_candidate = now
    if now.time() < A_SHARE_DAILY_DATA_READY_TIME:
        cutoff_candidate = now - timedelta(days=1)
        cutoff_reason = (
            f"当前时间 {now.strftime('%H:%M')} 早于 "
            f"{A_SHARE_DAILY_DATA_READY_TIME.strftime('%H:%M')}，"
            "默认使用上一已完成交易日行情。"
        )
    else:
        cutoff_reason = (
            f"当前时间 {now.strftime('%H:%M')} 已到收盘后数据可用窗口，"
            "优先使用今日收盘行情；若今日非交易日则使用最近已完成交易日。"
        )

    calendar_end = cutoff_candidate.strftime("%Y%m%d")
    calendar_start = (cutoff_candidate - timedelta(days=30)).strftime("%Y%m%d")
    cal = pro.trade_cal(
        exchange=exchange,
        start_date=calendar_start,
        end_date=calendar_end,
        is_open="1",
        fields="cal_date,is_open",
    )
    if cal is None or cal.empty:
        raise RuntimeError("无法从 Tushare trade_cal 获取最近已完成交易日。")

    trade_dates = (
        cal[cal["is_open"].astype(str) == "1"]["cal_date"]
        .astype(str)
        .sort_values()
        .tolist()
    )
    if not trade_dates:
        raise RuntimeError("Tushare trade_cal 未返回已开市交易日。")

    resolved_end_date = trade_dates[-1]
    if resolved_end_date == today_text and now.time() >= A_SHARE_DAILY_DATA_READY_TIME:
        suffix = "本次更新会使用今日收盘后数据来预测下一交易日。"
    else:
        suffix = f"本次更新截止到 {resolved_end_date}，用于预测其后的下一交易日。"

    return resolved_end_date, f"{cutoff_reason}{suffix}"


def get_recent_trade_dates(
    pro,
    recent_trade_days: int = 10,
    end_date: str | None = None,
    exchange: str = "SSE",
) -> list[str]:
    if recent_trade_days <= 0:
        raise ValueError("recent_trade_days 必须大于 0。")

    if end_date is None:
        end_date = datetime.today().strftime("%Y%m%d")

    end_dt = datetime.strptime(end_date, "%Y%m%d")
    lookback_days = max(45, recent_trade_days * 4)

    for _ in range(4):
        start_date = (end_dt - timedelta(days=lookback_days)).strftime("%Y%m%d")
        cal = pro.trade_cal(
            exchange=exchange,
            start_date=start_date,
            end_date=end_date,
            is_open="1",
            fields="cal_date,is_open",
        )

        if cal is not None and not cal.empty:
            dates = (
                cal[cal["is_open"].astype(str) == "1"]["cal_date"]
                .astype(str)
                .sort_values()
                .tail(recent_trade_days)
                .tolist()
            )

            if len(dates) >= recent_trade_days:
                return dates

        lookback_days *= 2

    raise RuntimeError("没有获取到足够的最近交易日，请检查 Tushare trade_cal 接口。")


def _resolve_daily_fetch_workers(max_workers: int | None, task_count: int) -> int:
    if task_count <= 0:
        return 1
    if max_workers is None:
        env_value = os.environ.get("TUSHARE_DAILY_FETCH_WORKERS", "").strip()
        if env_value:
            try:
                max_workers = int(float(env_value))
            except ValueError:
                max_workers = None
    if max_workers is None:
        max_workers = min(4, task_count)
    return max(1, min(int(max_workers), task_count))


def _fetch_one_trade_date_daily_fast(
    token: str,
    trade_date: str,
    ts_codes: set[str],
    include_turnover: bool,
    include_adj_factor: bool,
    max_retries: int,
) -> tuple[str, pd.DataFrame, str]:
    pro = init_tushare_pro(token)
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            df = pro.daily(
                trade_date=trade_date,
                fields=(
                    "ts_code,trade_date,open,high,low,close,"
                    "pct_chg,vol,amount"
                ),
            )

            if df is None or df.empty:
                return trade_date, pd.DataFrame(), "empty_daily"

            df = df[df["ts_code"].isin(ts_codes)].copy()
            if df.empty:
                return trade_date, pd.DataFrame(), "empty_stock_pool"

            if include_turnover:
                try:
                    basic = pro.daily_basic(
                        trade_date=trade_date,
                        fields="ts_code,trade_date,turnover_rate",
                    )

                    if basic is not None and not basic.empty:
                        df = df.merge(
                            basic,
                            on=["ts_code", "trade_date"],
                            how="left",
                        )
                except Exception as e:
                    print(f"[Warning] daily_basic failed, trade_date={trade_date}: {e}")

            if include_adj_factor:
                try:
                    adj = pro.adj_factor(
                        trade_date=trade_date,
                        fields="ts_code,trade_date,adj_factor",
                    )

                    if adj is not None and not adj.empty:
                        df = df.merge(
                            adj,
                            on=["ts_code", "trade_date"],
                            how="left",
                        )
                except Exception as e:
                    print(f"[Warning] adj_factor failed, trade_date={trade_date}: {e}")

            return trade_date, df, "ok"

        except Exception as e:
            last_error = e
            wait_seconds = 1.0 + attempt * 1.5 + random.random()
            print(
                f"[Retry {attempt}/{max_retries}] "
                f"trade_date={trade_date} failed: {e}"
            )
            time.sleep(wait_seconds)

    return trade_date, pd.DataFrame(), f"failed:{last_error}"


def fetch_stock_pool_recent_daily_fast(
    token: str,
    stock_pool: dict | None = None,
    recent_trade_days: int = 10,
    end_date: str | None = None,
    include_adj_factor: bool = False,
    include_turnover: bool = True,
    max_workers: int | None = None,
    max_retries: int = 3,
    sleep_seconds: float = 0.2,
) -> pd.DataFrame:
    """
    按交易日批量获取最近日线。

    请求量约为最近交易日数量，而不是股票数量，适合 CSI300 每日更新。
    """
    pro = init_tushare_pro(token)
    resolved_end_date, cutoff_message = resolve_daily_data_end_date(
        pro=pro,
        end_date=end_date,
    )
    print(f"[Fetch Fast] data cutoff end_date={resolved_end_date}")
    print(f"[Fetch Fast] cutoff rule: {cutoff_message}")

    if stock_pool is None:
        stock_pool = get_stock_pool(token=token, enrich_name=True)

    code_name_map = {format_code(code): name for code, name in stock_pool.items()}
    ts_codes = {to_ts_code(code) for code in code_name_map}

    trade_dates = get_recent_trade_dates(
        pro=pro,
        recent_trade_days=recent_trade_days,
        end_date=resolved_end_date,
    )

    print(f"[Fetch Fast] recent trade dates = {trade_dates}")
    print(f"[Fetch Fast] stock pool size = {len(ts_codes)}")

    worker_count = _resolve_daily_fetch_workers(max_workers, len(trade_dates))
    print(f"[Fetch Fast] parallel workers = {worker_count}")

    fetched_by_date: dict[str, pd.DataFrame] = {}

    if worker_count <= 1:
        for i, trade_date in enumerate(trade_dates, start=1):
            print(f"[Fetch Fast] {i}/{len(trade_dates)} trade_date={trade_date}")
            date, df, status = _fetch_one_trade_date_daily_fast(
                token=token,
                trade_date=trade_date,
                ts_codes=ts_codes,
                include_turnover=include_turnover,
                include_adj_factor=include_adj_factor,
                max_retries=max_retries,
            )
            if status == "ok" and not df.empty:
                fetched_by_date[date] = df
                print(f"[OK][Fetch Fast] trade_date={date}, rows={len(df)}")
            else:
                print(f"[Warning][Fetch Fast] trade_date={date}, status={status}")

            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    _fetch_one_trade_date_daily_fast,
                    token,
                    trade_date,
                    ts_codes,
                    include_turnover,
                    include_adj_factor,
                    max_retries,
                ): trade_date
                for trade_date in trade_dates
            }
            completed = 0
            for future in as_completed(future_map):
                requested_date = future_map[future]
                completed += 1
                try:
                    date, df, status = future.result()
                except Exception as e:
                    print(f"[Error][Fetch Fast] trade_date={requested_date}, failed: {e}")
                    continue

                if status == "ok" and not df.empty:
                    fetched_by_date[date] = df
                    print(
                        f"[OK][Fetch Fast] {completed}/{len(trade_dates)} "
                        f"trade_date={date}, rows={len(df)}"
                    )
                else:
                    print(
                        f"[Warning][Fetch Fast] {completed}/{len(trade_dates)} "
                        f"trade_date={date}, status={status}"
                    )

    all_data = [
        fetched_by_date[trade_date]
        for trade_date in trade_dates
        if trade_date in fetched_by_date
    ]

    if not all_data:
        raise RuntimeError("没有获取到任何最近日线数据。")

    data = pd.concat(all_data, ignore_index=True)

    data["code"] = data["ts_code"].map(ts_code_to_code)
    data["name"] = data["code"].map(code_name_map).fillna(data["code"])
    data = data.rename(
        columns={
            "trade_date": "date",
            "vol": "volume",
            "turnover_rate": "turnover",
        }
    )

    data["date"] = pd.to_datetime(data["date"], format="%Y%m%d")

    numeric_cols = [
        "open",
        "close",
        "high",
        "low",
        "volume",
        "amount",
        "pct_chg",
        "turnover",
        "adj_factor",
    ]

    for col in numeric_cols:
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")

    if "turnover" not in data.columns:
        data["turnover"] = 0.0
    else:
        data["turnover"] = data["turnover"].fillna(0.0)

    data["vwap"] = (data["amount"] * 1000.0) / (data["volume"] * 100.0 + EPS)
    data.loc[~np.isfinite(data["vwap"]) | (data["volume"].fillna(0) <= 0), "vwap"] = data["close"]

    output_cols = [
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

    if "adj_factor" in data.columns:
        output_cols.append("adj_factor")

    data = data[output_cols].copy()
    data["code"] = data["code"].astype(str).str.zfill(6)
    data = data.dropna(subset=["open", "close", "high", "low"])
    data = data.drop_duplicates(subset=["code", "date"], keep="last")
    data = data.sort_values(["code", "date"]).reset_index(drop=True)

    print(f"[Fetch Fast] final shape = {data.shape}")
    print(f"[Fetch Fast] date range = {data['date'].min()} ~ {data['date'].max()}")

    return data


def fetch_one_stock_tushare(
    code: str,
    name: str,
    token: str,
    start_date: str = START_DATE,
    end_date: str | None = None,
    max_retries: int = 5,
) -> pd.DataFrame:
    init_tushare(token)

    code = format_code(code)
    ts_code = to_ts_code(code)

    if end_date is None:
        end_date = datetime.today().strftime("%Y%m%d")

    print(f"[Fetch][Tushare] {code} {name} {ts_code}")

    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            df = _tushare_module().pro_bar(
                ts_code=ts_code,
                asset="E",
                adj="qfq",
                freq="D",
                start_date=start_date,
                end_date=end_date,
            )

            if df is None or df.empty:
                print(f"[Warning] {code} {name} empty data")
                return pd.DataFrame()

            df = df.rename(columns={
                "trade_date": "date",
                "vol": "volume",
            })

            needed_cols = [
                "date", "open", "close", "high", "low",
                "volume", "amount", "pct_chg"
            ]

            df = df[[c for c in needed_cols if c in df.columns]].copy()

            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
            df["code"] = code
            df["name"] = name

            numeric_cols = [
                "open", "close", "high", "low",
                "volume", "amount", "pct_chg"
            ]

            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            # Tushare amount 通常是千元，volume 通常是手
            if "amount" in df.columns and "volume" in df.columns:
                df["vwap"] = (df["amount"] * 1000.0) / (df["volume"] * 100.0 + EPS)
            else:
                df["vwap"] = df["close"]

            # 当前版本 Alpha158 不使用 turnover，先置 0
            df["turnover"] = 0.0

            df = df.sort_values("date").reset_index(drop=True)

            print(f"[OK][Tushare] {code} {name}, rows={len(df)}")
            return df

        except Exception as e:
            last_error = e
            wait_seconds = 2 + attempt * 2 + random.random() * 2
            print(f"[Retry {attempt}/{max_retries}] {code} {name} failed: {e}")
            print(f"[Sleep] {wait_seconds:.1f}s")
            time.sleep(wait_seconds)

    print(f"[Error] {code} {name} final failed: {last_error}")
    return pd.DataFrame()


def fetch_stock_pool_tushare(
    token: str,
    stock_pool: dict | None = None,
    start_date: str = START_DATE,
    end_date: str | None = None,
    cache_path: str | None = None,
    use_cache_when_fail: bool = True,
) -> pd.DataFrame:
    init_tushare(token)

    if stock_pool is None:
        stock_pool = get_stock_pool(token=token, enrich_name=True)

    if end_date is None:
        end_date = datetime.today().strftime("%Y%m%d")

    all_data = []

    for i, (code, name) in enumerate(stock_pool.items(), start=1):
        print(f"[Progress] {i}/{len(stock_pool)}")

        df = fetch_one_stock_tushare(
            code=code,
            name=name,
            token=token,
            start_date=start_date,
            end_date=end_date,
        )

        if not df.empty:
            all_data.append(df)

        sleep_seconds = 1.0 + random.random() * 1.5
        print(f"[Sleep] {sleep_seconds:.1f}s before next stock")
        time.sleep(sleep_seconds)

    if all_data:
        data = pd.concat(all_data, ignore_index=True)
        data = data.sort_values(["code", "date"]).reset_index(drop=True)

        if cache_path:
            data.to_csv(cache_path, index=False, encoding="utf-8-sig")
            print(f"[Save] data -> {cache_path}, shape={data.shape}")

        return data

    if use_cache_when_fail and cache_path and os.path.exists(cache_path):
        print(f"[Cache] online fetch failed, use cache: {cache_path}")
        data = pd.read_csv(cache_path, dtype={"code": str})
        data["code"] = data["code"].astype(str).str.zfill(6)
        data["date"] = pd.to_datetime(data["date"])
        return data

    raise RuntimeError(
        "没有获取到任何股票数据。请检查 Tushare token、接口权限、网络或调用频率。"
    )
