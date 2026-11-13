# universe
from __future__ import annotations

from core.user_scope import owned_path

import calendar
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from config import (
    CSI300_AKSHARE_FALLBACK_ENABLED,
    CSI300_INDEX_WEIGHT_LOOKBACK_MONTHS,
    CSI300_POOL_CACHE_MAX_AGE_DAYS,
    CSI300_POOL_CACHE_PATH,
    CSI300_POOL_LAST_GOOD_PATH,
    QLIB_PROVIDER_URI,
    STOCK_POOL,
    UNIVERSE,
    USE_TUSHARE_INDEX_WEIGHT_FALLBACK,
)


CSI300_MIN_CONSTITUENTS = 250
CSI300_MAX_CONSTITUENTS = 350
CSI300_INDEX_CODES = ("000300.SH", "399300.SZ")


def format_code(code) -> str:
    return str(code).split(".")[0].strip().zfill(6)


def qlib_inst_to_code(inst: str) -> str:
    inst = str(inst).strip()

    if inst.startswith(("SH", "SZ", "BJ", "sh", "sz", "bj")):
        return inst[2:].zfill(6)

    return inst.zfill(6)


def code_to_ts_code(code: str) -> str:
    code = format_code(code)

    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"

    if code.startswith(("5", "6", "9")):
        return f"{code}.SH"

    if code.startswith(("4", "8")):
        return f"{code}.BJ"

    raise ValueError(f"无法识别交易所：{code}")


def ts_code_to_code(ts_code: str) -> str:
    return str(ts_code).split(".")[0].zfill(6)


def _validate_pool_size(size: int, source: str) -> None:
    if not CSI300_MIN_CONSTITUENTS <= size <= CSI300_MAX_CONSTITUENTS:
        raise ValueError(
            f"CSI300 股票池数量异常：{size}，source={source}，"
            f"合理范围为 {CSI300_MIN_CONSTITUENTS}~{CSI300_MAX_CONSTITUENTS}。"
        )


def _normalize_pool_frame(
    frame: pd.DataFrame,
    *,
    source: str,
    effective_date: str | None = None,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError(f"CSI300 股票池为空：source={source}")

    df = frame.copy()

    if "code" not in df.columns:
        raise ValueError(f"CSI300 股票池缺少 code 字段：source={source}")

    df["code"] = (
        df["code"]
        .astype(str)
        .str.strip()
        .str.split(".")
        .str[0]
        .str.zfill(6)
    )
    df = df[df["code"].str.fullmatch(r"\d{6}", na=False)].copy()
    df = df.drop_duplicates(subset=["code"], keep="last")

    if "name" not in df.columns:
        df["name"] = df["code"]
    else:
        df["name"] = df["name"].fillna(df["code"]).astype(str).str.strip()
        numeric_name = df["name"].str.fullmatch(r"\d+")
        df.loc[numeric_name, "name"] = (
            df.loc[numeric_name, "name"].str.zfill(6)
        )
        df.loc[df["name"] == "", "name"] = df.loc[df["name"] == "", "code"]

    if "ts_code" not in df.columns:
        df["ts_code"] = df["code"].map(code_to_ts_code)
    else:
        df["ts_code"] = df["ts_code"].fillna("").astype(str).str.strip()
        missing = ~df["ts_code"].str.contains(r"\.", regex=True)
        df.loc[missing, "ts_code"] = df.loc[missing, "code"].map(
            code_to_ts_code
        )

    _validate_pool_size(len(df), source)

    df["source"] = source
    df["effective_date"] = str(
        effective_date or datetime.now().strftime("%Y-%m-%d")
    )
    df["fetched_at"] = datetime.now().astimezone().isoformat(timespec="seconds")

    return (
        df[
            [
                "code",
                "ts_code",
                "name",
                "source",
                "effective_date",
                "fetched_at",
            ]
        ]
        .sort_values("code")
        .reset_index(drop=True)
    )


def _atomic_write_csv(frame: pd.DataFrame, path: str) -> None:
    destination = owned_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(fd)
    temporary = Path(temporary_name)

    try:
        frame.to_csv(temporary, index=False, encoding="utf-8-sig")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _save_pool_cache(frame: pd.DataFrame) -> None:
    _atomic_write_csv(frame, CSI300_POOL_CACHE_PATH)
    _atomic_write_csv(frame, CSI300_POOL_LAST_GOOD_PATH)

    print(f"[Universe] saved -> {CSI300_POOL_CACHE_PATH}")
    print(f"[Universe] last-good -> {CSI300_POOL_LAST_GOOD_PATH}")


def _cache_age_days(path: str) -> float:
    modified = datetime.fromtimestamp(owned_path(path).stat().st_mtime)
    return max(0.0, (datetime.now() - modified).total_seconds() / 86400.0)


def load_cached_csi300_pool(
    path: str | None = None,
) -> dict[str, str]:
    cache_path = owned_path(path or CSI300_POOL_CACHE_PATH)
    if not os.path.exists(cache_path):
        raise FileNotFoundError(cache_path)

    df = pd.read_csv(
        cache_path,
        dtype={
            "code": str,
            "ts_code": str,
            "name": str,
            "source": str,
            "effective_date": str,
            "fetched_at": str,
        },
    )
    normalized = _normalize_pool_frame(
        df,
        source=str(
            df["source"].dropna().iloc[-1]
            if "source" in df.columns and df["source"].notna().any()
            else f"cache:{cache_path}"
        ),
        effective_date=str(
            df["effective_date"].dropna().iloc[-1]
            if (
                "effective_date" in df.columns
                and df["effective_date"].notna().any()
            )
            else datetime.fromtimestamp(
                Path(cache_path).stat().st_mtime
            ).strftime("%Y-%m-%d")
        ),
    )
    return dict(zip(normalized["code"], normalized["name"]))


def _pool_to_frame(
    pool: dict[str, str],
    *,
    source: str,
    effective_date: str,
) -> pd.DataFrame:
    return _normalize_pool_frame(
        pd.DataFrame(
            [
                {
                    "code": format_code(code),
                    "ts_code": code_to_ts_code(code),
                    "name": name,
                }
                for code, name in pool.items()
            ]
        ),
        source=source,
        effective_date=effective_date,
    )


def read_csi300_from_qlib_instruments() -> dict[str, str]:
    """
    从 Qlib instruments/csi300.txt 读取 CSI300 股票池。

    常见文件：
    D:/qlib_data/cn_data/instruments/csi300.txt
    """
    candidates = [
        os.path.join(QLIB_PROVIDER_URI, "instruments", "csi300.txt"),
        os.path.join(QLIB_PROVIDER_URI, "instruments", "CSI300.txt"),
    ]

    inst_path = next((path for path in candidates if os.path.exists(path)), None)

    if inst_path is None:
        raise FileNotFoundError(
            "没有找到 Qlib CSI300 股票池文件。已尝试：\n"
            + "\n".join(candidates)
        )

    raw_rows: list[dict[str, Any]] = []
    with open(inst_path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue

            parts = line.split()
            inst = parts[0]
            code = qlib_inst_to_code(inst)
            raw_rows.append(
                {
                    "inst": inst,
                    "code": code,
                    "ts_code": code_to_ts_code(code),
                    "name": code,
                    "start": parts[1] if len(parts) >= 2 else None,
                    "end": parts[2] if len(parts) >= 3 else None,
                }
            )

    df_all = pd.DataFrame(raw_rows)
    if df_all.empty:
        raise RuntimeError(f"Qlib CSI300 股票池文件为空：{inst_path}")

    if df_all["start"].notna().any() and df_all["end"].notna().any():
        today = datetime.today().strftime("%Y-%m-%d")
        active = df_all[
            (df_all["start"].fillna("") <= today)
            & (df_all["end"].fillna("9999-12-31") >= today)
        ].copy()

        if active.empty:
            effective_date = str(df_all["end"].dropna().max())
            active = df_all[
                (df_all["start"].fillna("") <= effective_date)
                & (
                    df_all["end"].fillna("9999-12-31")
                    >= effective_date
                )
            ].copy()
        else:
            effective_date = today
    else:
        active = df_all.copy()
        effective_date = "all"

    frame = _normalize_pool_frame(
        active,
        source=f"qlib_csi300_{effective_date}",
        effective_date=effective_date,
    )
    _save_pool_cache(frame)

    print(
        f"[Universe] CSI300 from Qlib: {len(frame)} stocks, "
        f"effective_date={effective_date}"
    )
    return dict(zip(frame["code"], frame["name"]))


def enrich_names_with_tushare(
    token: str,
    stock_pool: dict[str, str],
) -> dict[str, str]:
    """
    用 Tushare stock_basic 补充股票名称。
    如果失败，不影响主流程，仍使用原名称。
    """
    try:
        import tushare as ts

        ts.set_token(token)
        pro = ts.pro_api(token)
        basic = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name",
        )

        if basic is None or basic.empty:
            return stock_pool

        basic["symbol"] = basic["symbol"].astype(str).str.zfill(6)
        name_map = dict(zip(basic["symbol"], basic["name"]))

        return {
            format_code(code): str(
                name_map.get(format_code(code), old_name)
            )
            for code, old_name in stock_pool.items()
        }

    except Exception as exc:
        print(f"[Universe] enrich names failed, keep original names: {exc}")
        return stock_pool


def _month_window(reference: datetime, months_back: int) -> tuple[str, str]:
    year = reference.year
    month = reference.month - months_back
    while month <= 0:
        year -= 1
        month += 12

    last_day = calendar.monthrange(year, month)[1]
    return (
        f"{year:04d}{month:02d}01",
        f"{year:04d}{month:02d}{last_day:02d}",
    )


def _looks_like_permission_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "权限",
            "积分",
            "permission",
            "privilege",
            "没有访问",
            "抱歉",
        )
    )


def read_csi300_from_tushare_index_weight(
    token: str,
) -> dict[str, str]:
    """
    从 Tushare index_weight 获取最近一期 CSI300 成分股。

    index_weight 是月度数据，因此按自然月逐月回溯，而不是用一段跨月日期
    假设接口一定返回数据。当前月无快照时会继续查询前一个月。
    """
    import tushare as ts

    ts.set_token(token)
    pro = ts.pro_api(token)

    errors: list[str] = []
    permission_error: Exception | None = None
    today = datetime.today()

    for months_back in range(max(1, CSI300_INDEX_WEIGHT_LOOKBACK_MONTHS)):
        start_date, end_date = _month_window(today, months_back)

        for index_code in CSI300_INDEX_CODES:
            try:
                df = pro.index_weight(
                    index_code=index_code,
                    start_date=start_date,
                    end_date=end_date,
                    fields="index_code,con_code,trade_date,weight",
                )
            except Exception as exc:
                errors.append(
                    f"{index_code} {start_date}-{end_date}: {exc}"
                )
                if _looks_like_permission_error(exc):
                    permission_error = exc
                    break
                continue

            if df is None or df.empty:
                errors.append(
                    f"{index_code} {start_date}-{end_date}: empty"
                )
                continue

            required = {"con_code", "trade_date"}
            if not required.issubset(df.columns):
                errors.append(
                    f"{index_code} {start_date}-{end_date}: "
                    f"missing columns={sorted(required - set(df.columns))}"
                )
                continue

            df = df.copy()
            df["trade_date"] = df["trade_date"].astype(str)
            latest_date = df["trade_date"].max()
            latest = (
                df[df["trade_date"] == latest_date]
                .drop_duplicates(subset=["con_code"], keep="last")
                .copy()
            )
            latest["code"] = latest["con_code"].map(ts_code_to_code)
            latest["ts_code"] = latest["con_code"].astype(str)
            latest["name"] = latest["code"]

            try:
                frame = _normalize_pool_frame(
                    latest,
                    source=(
                        f"tushare_index_weight_{index_code}_{latest_date}"
                    ),
                    effective_date=latest_date,
                )
            except ValueError as exc:
                errors.append(
                    f"{index_code} {latest_date}: invalid snapshot: {exc}"
                )
                continue

            pool = dict(zip(frame["code"], frame["name"]))
            pool = enrich_names_with_tushare(token, pool)
            frame = _pool_to_frame(
                pool,
                source=f"tushare_index_weight_{index_code}_{latest_date}",
                effective_date=latest_date,
            )
            _save_pool_cache(frame)

            print(
                "[Universe] CSI300 from Tushare index_weight: "
                f"{len(pool)} stocks, index_code={index_code}, "
                f"effective_date={latest_date}"
            )
            return pool

        if permission_error is not None:
            break

    detail = " | ".join(errors[-8:])
    if permission_error is not None:
        raise RuntimeError(
            "Tushare index_weight 权限不足或积分不满足要求；"
            "将继续尝试 AKShare 或本地 last-good 缓存。"
            f" 原始错误：{permission_error}"
        )

    raise RuntimeError(
        "Tushare index_weight 在逐月回溯后仍未返回有效 CSI300 快照。"
        f" lookback_months={CSI300_INDEX_WEIGHT_LOOKBACK_MONTHS}; "
        f"detail={detail}"
    )


def read_csi300_from_akshare() -> dict[str, str]:
    """
    从 AKShare 获取当前 CSI300 成分股。

    优先使用中证指数官网数据接口，失败后再使用新浪最新成分接口。
    """
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("当前环境缺少 akshare，无法使用备用股票池。") from exc

    errors: list[str] = []

    candidates = [
        (
            "akshare_csindex",
            getattr(ak, "index_stock_cons_csindex", None),
            {"symbol": "000300"},
            "成分券代码",
            "成分券名称",
            "日期",
        ),
        (
            "akshare_sina",
            getattr(ak, "index_stock_cons", None),
            {"symbol": "000300"},
            "品种代码",
            "品种名称",
            "纳入日期",
        ),
    ]

    for source, function, kwargs, code_column, name_column, date_column in candidates:
        if function is None:
            errors.append(f"{source}: function unavailable")
            continue

        try:
            df = function(**kwargs)
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            continue

        if df is None or df.empty:
            errors.append(f"{source}: empty")
            continue

        if code_column not in df.columns:
            errors.append(
                f"{source}: missing code column={code_column}; "
                f"columns={list(df.columns)}"
            )
            continue

        effective_date = datetime.today().strftime("%Y-%m-%d")
        if date_column in df.columns and df[date_column].notna().any():
            effective_date = str(df[date_column].dropna().iloc[0])

        raw = pd.DataFrame(
            {
                "code": df[code_column].astype(str),
                "name": (
                    df[name_column].astype(str)
                    if name_column in df.columns
                    else df[code_column].astype(str)
                ),
            }
        )

        try:
            frame = _normalize_pool_frame(
                raw,
                source=source,
                effective_date=effective_date,
            )
        except ValueError as exc:
            errors.append(f"{source}: {exc}")
            continue

        _save_pool_cache(frame)
        print(
            f"[Universe] CSI300 from {source}: {len(frame)} stocks, "
            f"effective_date={effective_date}"
        )
        return dict(zip(frame["code"], frame["name"]))

    raise RuntimeError(
        "AKShare 未能返回有效 CSI300 成分股。"
        f" detail={' | '.join(errors[-6:])}"
    )


def _try_cached_pool(
    path: str,
    *,
    enrich_name: bool,
    token: str | None,
) -> tuple[dict[str, str] | None, str | None]:
    if not os.path.exists(path):
        return None, None

    try:
        pool = load_cached_csi300_pool(path)
        if enrich_name and token:
            pool = enrich_names_with_tushare(token, pool)
        return pool, None
    except Exception as exc:
        return None, f"{path}: {exc}"


def get_stock_pool(
    token: str | None = None,
    enrich_name: bool = False,
) -> dict[str, str]:
    """
    统一股票池入口。

    可用性优先级：

    1. 有效且未过期的当前缓存；
    2. Qlib instruments；
    3. Tushare index_weight 按月回溯；
    4. AKShare 中证指数/新浪成分；
    5. 过期但有效的当前缓存或 last-good 缓存。

    在线数据源短暂失败时，不会让完整日更因为股票池不可用而直接终止。
    """
    universe = UNIVERSE.lower().strip()

    if universe == "manual":
        return STOCK_POOL

    if universe != "csi300":
        raise ValueError(f"不支持的 UNIVERSE：{UNIVERSE}")

    errors: list[str] = []
    stale_pool: dict[str, str] | None = None
    stale_source: str | None = None

    current_pool, current_error = _try_cached_pool(
        CSI300_POOL_CACHE_PATH,
        enrich_name=enrich_name,
        token=token,
    )
    if current_error:
        errors.append(current_error)

    if current_pool is not None:
        age_days = _cache_age_days(CSI300_POOL_CACHE_PATH)
        if age_days <= max(0, CSI300_POOL_CACHE_MAX_AGE_DAYS):
            print(
                f"[Universe] CSI300 from cache: {len(current_pool)} stocks, "
                f"age_days={age_days:.1f}"
            )
            return current_pool

        stale_pool = current_pool
        stale_source = (
            f"stale cache {CSI300_POOL_CACHE_PATH}, age_days={age_days:.1f}"
        )
        print(
            f"[Universe] CSI300 cache is stale: age_days={age_days:.1f}, "
            "try refreshing before fallback."
        )

    last_good_pool, last_good_error = _try_cached_pool(
        CSI300_POOL_LAST_GOOD_PATH,
        enrich_name=enrich_name,
        token=token,
    )
    if last_good_error:
        errors.append(last_good_error)
    if stale_pool is None and last_good_pool is not None:
        stale_pool = last_good_pool
        stale_source = f"last-good cache {CSI300_POOL_LAST_GOOD_PATH}"

    try:
        pool = read_csi300_from_qlib_instruments()
        if enrich_name and token:
            pool = enrich_names_with_tushare(token, pool)
            _save_pool_cache(
                _pool_to_frame(
                    pool,
                    source="qlib_csi300_tushare_name",
                    effective_date=datetime.today().strftime("%Y-%m-%d"),
                )
            )
        return pool
    except Exception as exc:
        errors.append(f"Qlib: {exc}")
        print(f"[Universe] read Qlib CSI300 failed: {exc}")

    if USE_TUSHARE_INDEX_WEIGHT_FALLBACK and token:
        try:
            return read_csi300_from_tushare_index_weight(token)
        except Exception as exc:
            errors.append(f"Tushare index_weight: {exc}")
            print(f"[Universe] Tushare CSI300 failed: {exc}")

    if CSI300_AKSHARE_FALLBACK_ENABLED:
        try:
            return read_csi300_from_akshare()
        except Exception as exc:
            errors.append(f"AKShare: {exc}")
            print(f"[Universe] AKShare CSI300 failed: {exc}")

    if stale_pool is not None:
        print(
            "[Universe][Fallback] all refresh sources failed; "
            f"use {stale_source}, stocks={len(stale_pool)}"
        )
        return stale_pool

    raise RuntimeError(
        "无法获取 CSI300 股票池。\n"
        f"Qlib 路径：{QLIB_PROVIDER_URI}\n"
        f"当前缓存：{CSI300_POOL_CACHE_PATH}\n"
        f"last-good 缓存：{CSI300_POOL_LAST_GOOD_PATH}\n"
        "请确认至少一个来源可用：Qlib、Tushare index_weight、AKShare。\n"
        f"详细错误：{' | '.join(errors[-8:])}"
    )
