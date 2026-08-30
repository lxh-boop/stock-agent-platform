import numpy as np
import pandas as pd

from config import (
    ALPHA_WINDOWS,
    EPS,
    LABEL_RET_CLIP,
    LABEL_ZSCORE_CLIP,
    MODEL_REG_LABEL_COL,
    PRED_HORIZON,
)


def safe_div(a, b):
    return a / (b + EPS)


def rolling_slope(x):
    if np.any(np.isnan(x)):
        return np.nan
    n = len(x)
    t = np.arange(n)
    try:
        return np.polyfit(t, x, 1)[0]
    except Exception:
        return np.nan


def rolling_rsqr(x):
    if np.any(np.isnan(x)):
        return np.nan
    n = len(x)
    t = np.arange(n)
    try:
        coef = np.polyfit(t, x, 1)
        y_hat = coef[0] * t + coef[1]
        ss_res = np.sum((x - y_hat) ** 2)
        ss_tot = np.sum((x - np.mean(x)) ** 2)
        if ss_tot < EPS:
            return 0.0
        return 1 - ss_res / ss_tot
    except Exception:
        return np.nan


def rolling_resi(x):
    if np.any(np.isnan(x)):
        return np.nan
    n = len(x)
    t = np.arange(n)
    try:
        coef = np.polyfit(t, x, 1)
        y_hat_last = coef[0] * t[-1] + coef[1]
        return x[-1] - y_hat_last
    except Exception:
        return np.nan


def rolling_rank_last(x):
    if np.any(np.isnan(x)):
        return np.nan
    last = x[-1]
    return np.mean(x <= last)


def days_since_max(x):
    if np.any(np.isnan(x)):
        return np.nan
    return len(x) - 1 - int(np.argmax(x))


def days_since_min(x):
    if np.any(np.isnan(x)):
        return np.nan
    return len(x) - 1 - int(np.argmin(x))


def add_alpha158_for_one_stock(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("date").copy()

    close = g["close"]
    open_ = g["open"]
    high = g["high"]
    low = g["low"]
    volume = g["volume"]
    vwap = g["vwap"]

    max_oc = pd.concat([open_, close], axis=1).max(axis=1)
    min_oc = pd.concat([open_, close], axis=1).min(axis=1)
    high_low_range = high - low

    # K 线形态 9 个
    g["KMID"] = safe_div(close - open_, open_)
    g["KLEN"] = safe_div(high - low, open_)
    g["KMID2"] = safe_div(close - open_, high_low_range)
    g["KUP"] = safe_div(high - max_oc, open_)
    g["KUP2"] = safe_div(high - max_oc, high_low_range)
    g["KLOW"] = safe_div(min_oc - low, open_)
    g["KLOW2"] = safe_div(min_oc - low, high_low_range)
    g["KSFT"] = safe_div(2 * close - high - low, open_)
    g["KSFT2"] = safe_div(2 * close - high - low, high_low_range)

    # 当日相对价格 4 个
    g["OPEN0"] = safe_div(open_, close)
    g["HIGH0"] = safe_div(high, close)
    g["LOW0"] = safe_div(low, close)
    g["VWAP0"] = safe_div(vwap, close)

    ret_1 = close.pct_change(1)

    price_diff = close.diff()
    price_abs_chg = price_diff.abs()
    up_price = price_diff.clip(lower=0)
    down_price = (-price_diff).clip(lower=0)

    volume_diff = volume.diff()
    volume_abs_chg = volume_diff.abs()
    up_volume = volume_diff.clip(lower=0)
    down_volume = (-volume_diff).clip(lower=0)

    price_ratio = safe_div(close, close.shift(1))
    volume_ratio_log = np.log(safe_div(volume, volume.shift(1)) + 1)
    weighted_abs_ret_volume = ret_1.abs() * volume

    for d in ALPHA_WINDOWS:
        g[f"ROC{d}"] = safe_div(close.shift(d), close)
        g[f"MA{d}"] = safe_div(close.rolling(d).mean(), close)
        g[f"STD{d}"] = safe_div(close.rolling(d).std(), close)

        g[f"BETA{d}"] = safe_div(
            close.rolling(d).apply(rolling_slope, raw=True),
            close
        )

        g[f"RSQR{d}"] = close.rolling(d).apply(rolling_rsqr, raw=True)

        g[f"RESI{d}"] = safe_div(
            close.rolling(d).apply(rolling_resi, raw=True),
            close
        )

        g[f"MAX{d}"] = safe_div(high.rolling(d).max(), close)
        g[f"MIN{d}"] = safe_div(low.rolling(d).min(), close)

        g[f"QTLU{d}"] = safe_div(close.rolling(d).quantile(0.8), close)
        g[f"QTLD{d}"] = safe_div(close.rolling(d).quantile(0.2), close)

        g[f"RANK{d}"] = close.rolling(d).apply(rolling_rank_last, raw=True)

        rolling_low = low.rolling(d).min()
        rolling_high = high.rolling(d).max()
        g[f"RSV{d}"] = safe_div(close - rolling_low, rolling_high - rolling_low)

        g[f"IMAX{d}"] = high.rolling(d).apply(days_since_max, raw=True) / d
        g[f"IMIN{d}"] = low.rolling(d).apply(days_since_min, raw=True) / d
        g[f"IMXD{d}"] = g[f"IMAX{d}"] - g[f"IMIN{d}"]

        g[f"CORR{d}"] = close.rolling(d).corr(np.log(volume + 1))
        g[f"CORD{d}"] = price_ratio.rolling(d).corr(volume_ratio_log)

        g[f"CNTP{d}"] = (close > close.shift(1)).astype(float).rolling(d).mean()
        g[f"CNTN{d}"] = (close < close.shift(1)).astype(float).rolling(d).mean()
        g[f"CNTD{d}"] = g[f"CNTP{d}"] - g[f"CNTN{d}"]

        g[f"SUMP{d}"] = safe_div(
            up_price.rolling(d).sum(),
            price_abs_chg.rolling(d).sum()
        )

        g[f"SUMN{d}"] = safe_div(
            down_price.rolling(d).sum(),
            price_abs_chg.rolling(d).sum()
        )

        g[f"SUMD{d}"] = safe_div(
            up_price.rolling(d).sum() - down_price.rolling(d).sum(),
            price_abs_chg.rolling(d).sum()
        )

        g[f"VMA{d}"] = safe_div(volume.rolling(d).mean(), volume)
        g[f"VSTD{d}"] = safe_div(volume.rolling(d).std(), volume)

        g[f"WVMA{d}"] = safe_div(
            weighted_abs_ret_volume.rolling(d).std(),
            weighted_abs_ret_volume.rolling(d).mean()
        )

        g[f"VSUMP{d}"] = safe_div(
            up_volume.rolling(d).sum(),
            volume_abs_chg.rolling(d).sum()
        )

        g[f"VSUMN{d}"] = safe_div(
            down_volume.rolling(d).sum(),
            volume_abs_chg.rolling(d).sum()
        )

        g[f"VSUMD{d}"] = safe_div(
            up_volume.rolling(d).sum() - down_volume.rolling(d).sum(),
            volume_abs_chg.rolling(d).sum()
        )

    g["future_5d_ret"] = close.shift(-PRED_HORIZON) / close - 1
    g["future_5d_up"] = (g["future_5d_ret"] > 0).astype(int)

    g["ret_5"] = close.pct_change(5)
    g["ret_20"] = close.pct_change(20)
    g["vol_20"] = close.pct_change(1).rolling(20).std()
    g["drawdown_20"] = close / close.rolling(20).max() - 1

    return g


def cross_sectional_zscore(s: pd.Series) -> pd.Series:
    out = pd.Series(np.nan, index=s.index, dtype=float)
    valid = s.dropna()

    if len(valid) < 2:
        return out

    std = valid.std(ddof=0)

    if pd.isna(std) or std < EPS:
        out.loc[valid.index] = 0.0
        return out

    out.loc[valid.index] = (valid - valid.mean()) / std
    return out


def add_future_score_label(data: pd.DataFrame) -> pd.DataFrame:
    data = data.copy()

    clipped_ret = data["future_5d_ret"].clip(-LABEL_RET_CLIP, LABEL_RET_CLIP)
    data[MODEL_REG_LABEL_COL] = (
        clipped_ret.groupby(data["date"]).transform(cross_sectional_zscore)
    )
    data[MODEL_REG_LABEL_COL] = data[MODEL_REG_LABEL_COL].clip(
        -LABEL_ZSCORE_CLIP,
        LABEL_ZSCORE_CLIP,
    )

    return data


def add_alpha158_features(df: pd.DataFrame) -> pd.DataFrame:
    results = []

    for code, g in df.groupby("code"):
        print(f"[Alpha158] {code}")
        out = add_alpha158_for_one_stock(g)
        results.append(out)

    data = pd.concat(results, ignore_index=True)
    data = data.replace([np.inf, -np.inf], np.nan)
    data = add_future_score_label(data)


    return data


def get_alpha158_feature_cols(df: pd.DataFrame):
    exclude_cols = {
        "date", "code", "name",
        "open", "close", "high", "low",
        "volume", "amount", "pct_chg", "turnover", "vwap",
        "future_5d_ret", "future_5d_up", MODEL_REG_LABEL_COL,
        "ret_5", "ret_20", "vol_20", "drawdown_20",
    }

    feature_cols = [
        c for c in df.columns
        if c not in exclude_cols and pd.api.types.is_numeric_dtype(df[c])
    ]

    return feature_cols


def prepare_model_data(data: pd.DataFrame, feature_cols):
    model_df = data.copy()

    if MODEL_REG_LABEL_COL not in model_df.columns:
        model_df = add_future_score_label(model_df)

    model_df = model_df.dropna(
        subset=feature_cols + [MODEL_REG_LABEL_COL, "future_5d_ret", "future_5d_up"]
    ).copy()

    model_df["future_5d_ret"] = model_df["future_5d_ret"].clip(
        -LABEL_RET_CLIP,
        LABEL_RET_CLIP,
    )
    model_df[MODEL_REG_LABEL_COL] = model_df[MODEL_REG_LABEL_COL].clip(
        -LABEL_ZSCORE_CLIP,
        LABEL_ZSCORE_CLIP,
    )

    return model_df
