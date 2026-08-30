from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def _keys(frame: pd.DataFrame) -> None:
    frame["code"] = frame["ts_code"].astype(str).str.split(".").str[0].str.zfill(6)
    frame["date"] = pd.to_datetime(
        frame["trade_date"].astype(str), format="%Y%m%d", errors="coerce"
    )


def _numeric(frame: pd.DataFrame, columns: list[str]) -> None:
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float32")


def _rolling(
    frame: pd.DataFrame,
    column: str,
    window: int,
    statistic: str,
    minimum: int,
) -> pd.Series:
    values = frame.groupby("code", sort=False)[column].rolling(
        window, min_periods=minimum
    )
    return getattr(values, statistic)().reset_index(level=0, drop=True).reindex(frame.index)


def _daily_basic_and_close_features(path: Path) -> pd.DataFrame:
    raw_columns = [
        "close",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "dv_ttm",
        "total_mv",
        "circ_mv",
    ]
    frame = pd.read_csv(
        path,
        dtype={"ts_code": str, "trade_date": str},
        usecols=["ts_code", "trade_date", *raw_columns],
    )
    _keys(frame)
    _numeric(frame, raw_columns)
    frame = frame.dropna(subset=["date", "code", "close"]).sort_values(
        ["code", "date"], kind="stable"
    )
    frame = frame.drop_duplicates(["date", "code"], keep="last").reset_index(drop=True)
    grouped = frame.groupby("code", sort=False)
    next_close = grouped["close"].shift(-1)
    frame["future_1d_ret_tushare"] = next_close / frame["close"] - 1.0
    frame["label_up_tushare"] = next_close.gt(frame["close"]).astype("float32").where(
        next_close.notna()
    )

    basic = {
        f"basic_{column}": frame[column].astype("float32") for column in raw_columns[1:]
    }
    basic["basic_log_close"] = np.log1p(frame["close"].clip(lower=0.0)).astype(
        "float32"
    )
    basic["basic_log_total_mv"] = np.log1p(frame["total_mv"].clip(lower=0.0)).astype(
        "float32"
    )
    basic["basic_log_circ_mv"] = np.log1p(frame["circ_mv"].clip(lower=0.0)).astype(
        "float32"
    )
    result = pd.concat(
        [
            frame.loc[
                :, ["date", "code", "future_1d_ret_tushare", "label_up_tushare"]
            ],
            pd.DataFrame(basic, index=frame.index),
        ],
        axis=1,
    )
    for column in [value for value in result.columns if value.startswith("basic_")]:
        result[f"cross_pct_{column}"] = (
            result.groupby("date", sort=False)[column]
            .rank(pct=True, method="average")
            .astype("float32")
        )

    frame["tech_ret_1"] = grouped["close"].pct_change(fill_method=None)
    technical: dict[str, pd.Series] = {"tech_ret_1": frame["tech_ret_1"]}
    for horizon in (2, 3, 5, 10, 20, 60, 120, 250):
        technical[f"tech_ret_{horizon}"] = (
            frame["close"] / grouped["close"].shift(horizon) - 1.0
        )
    gain = frame["tech_ret_1"].clip(lower=0.0)
    loss = (-frame["tech_ret_1"]).clip(lower=0.0)
    frame["_gain"] = gain
    frame["_loss"] = loss
    frame["_up"] = frame["tech_ret_1"].gt(0.0).astype("float32")
    for window in (5, 10, 20, 60, 120, 250):
        minimum = max(3, window // 2)
        close_mean = _rolling(frame, "close", window, "mean", minimum)
        close_min = _rolling(frame, "close", window, "min", minimum)
        close_max = _rolling(frame, "close", window, "max", minimum)
        technical[f"tech_close_to_ma_{window}"] = frame["close"] / close_mean - 1.0
        technical[f"tech_volatility_{window}"] = _rolling(
            frame, "tech_ret_1", window, "std", minimum
        )
        technical[f"tech_mean_return_{window}"] = _rolling(
            frame, "tech_ret_1", window, "mean", minimum
        )
        technical[f"tech_up_rate_{window}"] = _rolling(
            frame, "_up", window, "mean", minimum
        )
        technical[f"tech_drawdown_{window}"] = frame["close"] / close_max - 1.0
        technical[f"tech_close_position_{window}"] = (
            (frame["close"] - close_min) / (close_max - close_min).replace(0.0, np.nan)
        )
        turnover_mean = _rolling(frame, "turnover_rate", window, "mean", minimum)
        technical[f"tech_turnover_ratio_{window}"] = (
            frame["turnover_rate"] / turnover_mean.replace(0.0, np.nan)
        )
    for window in (6, 12, 24):
        gain_mean = _rolling(frame, "_gain", window, "mean", max(3, window // 2))
        loss_mean = _rolling(frame, "_loss", window, "mean", max(3, window // 2))
        relative_strength = gain_mean / loss_mean.replace(0.0, np.nan)
        technical[f"tech_rsi_{window}"] = 1.0 - 1.0 / (1.0 + relative_strength)

    technical_frame = pd.DataFrame(technical, index=frame.index).replace(
        [np.inf, -np.inf], np.nan
    )
    technical_frame = technical_frame.astype("float32")
    rank_columns = [
        column
        for column in technical_frame.columns
        if column.startswith(
            (
                "tech_ret_",
                "tech_volatility_",
                "tech_up_rate_",
                "tech_drawdown_",
                "tech_close_position_",
                "tech_turnover_ratio_",
                "tech_rsi_",
            )
        )
    ]
    for column in rank_columns:
        technical_frame[f"cross_pct_{column}"] = (
            technical_frame.groupby(frame["date"], sort=False)[column]
            .rank(pct=True, method="average")
            .astype("float32")
        )
    return pd.concat([result, technical_frame], axis=1)


def _moneyflow_features(path: Path) -> pd.DataFrame:
    amounts = [
        "buy_sm_amount",
        "sell_sm_amount",
        "buy_md_amount",
        "sell_md_amount",
        "buy_lg_amount",
        "sell_lg_amount",
        "buy_elg_amount",
        "sell_elg_amount",
        "net_mf_amount",
    ]
    frame = pd.read_csv(
        path,
        dtype={"ts_code": str, "trade_date": str},
        usecols=["ts_code", "trade_date", *amounts],
    )
    _keys(frame)
    _numeric(frame, amounts)
    frame = frame.dropna(subset=["date", "code"]).sort_values(
        ["code", "date"], kind="stable"
    ).drop_duplicates(["date", "code"], keep="last").reset_index(drop=True)
    gross = sum(frame[column].clip(lower=0.0) for column in amounts[:-1]).replace(
        0.0, np.nan
    )
    frame["flow_net_ratio"] = frame["net_mf_amount"] / gross
    frame["flow_institutional_ratio"] = (
        frame["buy_lg_amount"]
        - frame["sell_lg_amount"]
        + frame["buy_elg_amount"]
        - frame["sell_elg_amount"]
    ) / gross
    frame["flow_retail_ratio"] = (
        frame["buy_sm_amount"] - frame["sell_sm_amount"]
    ) / gross
    frame["flow_medium_ratio"] = (
        frame["buy_md_amount"] - frame["sell_md_amount"]
    ) / gross
    ratio_columns = [
        "flow_net_ratio",
        "flow_institutional_ratio",
        "flow_retail_ratio",
        "flow_medium_ratio",
    ]
    additions: dict[str, pd.Series] = {}
    for column in ratio_columns:
        for window in (5, 20, 60):
            additions[f"{column}_mean_{window}"] = _rolling(
                frame, column, window, "mean", max(3, window // 2)
            )
        additions[f"cross_pct_{column}"] = frame.groupby("date", sort=False)[
            column
        ].rank(pct=True, method="average")
    values = pd.concat(
        [frame.loc[:, ["date", "code", *ratio_columns]], pd.DataFrame(additions)], axis=1
    )
    for column in values.columns[2:]:
        values[column] = values[column].astype("float32")
    return values


def _margin_features(path: Path) -> pd.DataFrame:
    columns = ["rzye", "rqye", "rzmre", "rqyl", "rzche", "rqchl", "rqmcl", "rzrqye"]
    frame = pd.read_csv(
        path,
        dtype={"ts_code": str, "trade_date": str},
        usecols=["ts_code", "trade_date", *columns],
    )
    _keys(frame)
    _numeric(frame, columns)
    frame = frame.dropna(subset=["date", "code"]).sort_values(
        ["code", "date"], kind="stable"
    ).drop_duplicates(["date", "code"], keep="last").reset_index(drop=True)
    frame["margin_financing_net_buy_ratio"] = (
        frame["rzmre"] - frame["rzche"]
    ) / frame["rzye"].replace(0.0, np.nan)
    frame["margin_short_to_financing"] = frame["rqye"] / frame["rzye"].replace(
        0.0, np.nan
    )
    frame["margin_total_log"] = np.log1p(frame["rzrqye"].clip(lower=0.0))
    grouped = frame.groupby("code", sort=False)
    frame["margin_financing_balance_change"] = grouped["rzye"].pct_change(
        fill_method=None
    )
    frame["margin_short_balance_change"] = grouped["rqye"].pct_change(fill_method=None)
    ratio_columns = [
        "margin_financing_net_buy_ratio",
        "margin_short_to_financing",
        "margin_financing_balance_change",
        "margin_short_balance_change",
    ]
    additions: dict[str, pd.Series] = {}
    for column in ratio_columns:
        for window in (5, 20):
            additions[f"{column}_mean_{window}"] = _rolling(
                frame, column, window, "mean", max(3, window // 2)
            )
        additions[f"cross_pct_{column}"] = frame.groupby("date", sort=False)[
            column
        ].rank(pct=True, method="average")
    values = pd.concat(
        [
            frame.loc[:, ["date", "code", "margin_total_log", *ratio_columns]],
            pd.DataFrame(additions),
        ],
        axis=1,
    )
    values = values.replace([np.inf, -np.inf], np.nan)
    for column in values.columns[2:]:
        values[column] = values[column].astype("float32")
    return values


def _index_features(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"ts_code": str, "trade_date": str})
    _keys(frame)
    _numeric(frame, ["pct_chg", "vol", "amount"])
    frame["index_code"] = frame["ts_code"].str.split(".").str[0]
    wide = frame.pivot(index="date", columns="index_code", values=["pct_chg", "vol", "amount"])
    wide.columns = [f"market_index_{code}_{metric}" for metric, code in wide.columns]
    wide = wide.reset_index().sort_values("date").reset_index(drop=True)
    additions: dict[str, pd.Series] = {}
    for column in [value for value in wide.columns if value.endswith("_pct_chg")]:
        for window in (5, 20, 60):
            additions[f"{column}_mean_{window}"] = wide[column].rolling(
                window, min_periods=max(3, window // 2)
            ).mean()
        additions[f"{column}_volatility_20"] = wide[column].rolling(
            20, min_periods=10
        ).std()
    for column in [value for value in wide.columns if value.endswith(("_vol", "_amount"))]:
        for window in (5, 20, 60):
            average = wide[column].rolling(window, min_periods=max(3, window // 2)).mean()
            additions[f"{column}_ratio_{window}"] = wide[column] / average.replace(
                0.0, np.nan
            )
    result = pd.concat([wide, pd.DataFrame(additions)], axis=1)
    for column in result.columns[1:]:
        result[column] = pd.to_numeric(result[column], errors="coerce").astype("float32")
    return result


def _hsgt_features(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"trade_date": str})
    frame["date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="coerce")
    columns = [value for value in ("hgt", "sgt", "north_money", "south_money") if value in frame]
    _numeric(frame, columns)
    frame = frame.sort_values("date").reset_index(drop=True)
    additions: dict[str, pd.Series] = {}
    for column in columns:
        additions[f"market_hsgt_{column}"] = frame[column]
        for window in (5, 20, 60):
            additions[f"market_hsgt_{column}_mean_{window}"] = frame[column].rolling(
                window, min_periods=max(3, window // 2)
            ).mean()
    result = pd.concat([frame.loc[:, ["date"]], pd.DataFrame(additions)], axis=1)
    for column in result.columns[1:]:
        result[column] = result[column].astype("float32")
    return result


def build_tushare_panel(
    feature_dir: str | Path,
    *,
    start_date: str = "2018-02-14",
    end_date: str = "2026-07-30",
) -> tuple[pd.DataFrame, list[str], dict[str, Any]]:
    directory = Path(feature_dir)
    frame = _daily_basic_and_close_features(directory / "daily_basic.csv")
    frame = frame.merge(_moneyflow_features(directory / "moneyflow.csv"), on=["date", "code"], how="left")
    margin_path = directory / "margin_detail.csv"
    if margin_path.exists():
        frame = frame.merge(_margin_features(margin_path), on=["date", "code"], how="left")
    frame = frame.merge(_index_features(directory / "index_daily.csv"), on="date", how="left")
    hsgt_path = directory / "moneyflow_hsgt.csv"
    if hsgt_path.exists():
        frame = frame.merge(_hsgt_features(hsgt_path), on="date", how="left")
    frame = frame.sort_values(["date", "code"], kind="stable").reset_index(drop=True)

    daily_up = frame.groupby("date", sort=True)["label_up_tushare"].mean().shift(1)
    state: dict[str, pd.Series] = {}
    for window in (5, 20, 60):
        known = daily_up.rolling(window, min_periods=max(3, window // 2)).mean()
        state[f"market_prior_up_rate_{window}"] = frame["date"].map(known).astype(
            "float32"
        )
    state["is_shanghai"] = frame["code"].str.startswith(("6", "9")).astype("float32")
    state["is_chinext"] = frame["code"].str.startswith(("300", "301")).astype("float32")
    state["is_star"] = frame["code"].str.startswith(("688", "689")).astype("float32")
    state["is_beijing"] = frame["code"].str.startswith(("4", "8")).astype("float32")
    state["month"] = frame["date"].dt.month.astype("float32")
    state["weekday"] = frame["date"].dt.weekday.astype("float32")
    frame = pd.concat([frame, pd.DataFrame(state, index=frame.index)], axis=1)
    frame = frame[frame["date"].between(start_date, end_date)].copy()
    frame = frame.replace([np.inf, -np.inf], np.nan)

    excluded = {"date", "code", "future_1d_ret_tushare", "label_up_tushare"}
    candidates = [column for column in frame.columns if column not in excluded]
    coverage = frame[frame["date"].le("2024-12-31")]
    features = [
        column
        for column in candidates
        if coverage[column].notna().mean() >= 0.80
        and coverage[column].std(skipna=True) > 1e-8
    ]
    audit = {
        "rows": int(len(frame)),
        "stocks": int(frame["code"].nunique()),
        "days": int(frame["date"].nunique()),
        "start_date": str(frame["date"].min().date()),
        "end_date": str(frame["date"].max().date()),
        "duplicate_keys": int(frame.duplicated(["date", "code"]).sum()),
        "label_rows": int(frame["label_up_tushare"].notna().sum()),
        "feature_count": len(features),
        "coverage_rule": ">=80% non-null through 2024-12-31",
    }
    return frame.loc[:, ["date", "code", "future_1d_ret_tushare", "label_up_tushare", *features]], features, audit


__all__ = ["build_tushare_panel"]
