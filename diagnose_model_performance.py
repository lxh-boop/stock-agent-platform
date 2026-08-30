from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from alpha158 import get_alpha158_feature_cols, prepare_model_data
from config import (
    EVAL_METRICS_PATH,
    MODEL_PRED_COL,
    MODEL_REG_LABEL_COL,
    OUTPUT_DIR,
    PRED_HORIZON,
    RANKING_LATEST_PATH,
    TEST_PREDICTIONS_PATH,
    ensure_dirs,
)
from news_features import get_news_event_feature_cols
from universe import get_stock_pool
from factor_provider import training_alpha158


REPORT_PATH = Path(OUTPUT_DIR) / "model_diagnosis_report.md"
STATS_PATH = Path(OUTPUT_DIR) / "model_diagnosis_stats.json"


def read_csv_if_exists(path: str | os.PathLike) -> pd.DataFrame | None:
    path = Path(path)
    if not path.exists():
        return None
    return pd.read_csv(path, encoding="utf-8-sig")


def as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    except Exception:
        return None


def to_builtin(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): to_builtin(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_builtin(v) for v in value]
    if isinstance(value, tuple):
        return [to_builtin(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return as_float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    if pd.isna(value) if not isinstance(value, (list, dict, tuple)) else False:
        return None
    return value


def series_distribution(s: pd.Series, quantiles: list[float] | None = None) -> dict:
    quantiles = quantiles or [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]
    x = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    out = {
        "count": int(len(x)),
        "missing": int(len(s) - len(x)),
        "mean": None,
        "std": None,
        "min": None,
        "max": None,
        "quantiles": {},
    }
    if x.empty:
        return out
    out.update(
        {
            "mean": as_float(x.mean()),
            "std": as_float(x.std()),
            "min": as_float(x.min()),
            "max": as_float(x.max()),
            "quantiles": {str(q): as_float(x.quantile(q)) for q in quantiles},
        }
    )
    return out


def calc_ic(x: pd.Series, y: pd.Series) -> float | None:
    pair = pd.DataFrame({"x": x, "y": y}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(pair) < 3:
        return None
    if pair["x"].std(ddof=0) < 1e-12 or pair["y"].std(ddof=0) < 1e-12:
        return None
    return as_float(np.corrcoef(pair["x"].values, pair["y"].values)[0, 1])


def calc_rankic(x: pd.Series, y: pd.Series) -> float | None:
    pair = pd.DataFrame({"x": x, "y": y}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(pair) < 3:
        return None
    if pair["x"].nunique() < 2 or pair["y"].nunique() < 2:
        return None
    try:
        return as_float(spearmanr(pair["x"], pair["y"]).correlation)
    except Exception:
        return None


def summarize_metric(values: list[float | None]) -> dict:
    x = pd.Series([v for v in values if v is not None], dtype=float).dropna()
    if x.empty:
        return {"count": 0, "mean": None, "std": None, "ir": None}
    mean = x.mean()
    std = x.std()
    return {
        "count": int(len(x)),
        "mean": as_float(mean),
        "std": as_float(std),
        "ir": as_float(mean / std) if std and std > 1e-12 else None,
        "positive_ratio": as_float((x > 0).mean()),
    }


def analyze_data_scope(df: pd.DataFrame) -> dict:
    data = df.copy()
    data["date"] = pd.to_datetime(data["date"])
    data["code"] = data["code"].astype(str).str.zfill(6)

    stock_days = data.groupby("code")["date"].nunique()
    label_mask = data["future_5d_ret"].notna() if "future_5d_ret" in data.columns else pd.Series(False, index=data.index)
    return {
        "date_min": data["date"].min(),
        "date_max": data["date"].max(),
        "trading_days": int(data["date"].nunique()),
        "stock_count": int(data["code"].nunique()),
        "total_rows": int(len(data)),
        "labeled_rows": int(label_mask.sum()),
        "stock_trading_days": {
            "min": as_float(stock_days.min()),
            "p25": as_float(stock_days.quantile(0.25)),
            "median": as_float(stock_days.median()),
            "p75": as_float(stock_days.quantile(0.75)),
            "max": as_float(stock_days.max()),
        },
    }


def analyze_labels(df: pd.DataFrame) -> dict:
    data = df.copy()
    data["date"] = pd.to_datetime(data["date"])
    labeled = data[data["future_5d_ret"].notna()].copy()
    up = pd.to_numeric(labeled.get("future_5d_up"), errors="coerce")

    daily = (
        labeled.groupby("date")["future_5d_ret"]
        .agg(["count", "mean", "std", "min", "max"])
        .reset_index()
    )

    return {
        "future_5d_ret": series_distribution(labeled["future_5d_ret"]),
        "future_5d_up": {
            "positive": int((up == 1).sum()),
            "negative": int((up == 0).sum()),
            "positive_ratio": as_float((up == 1).mean()),
        },
        "daily_cross_section": {
            "valid_dates": int(len(daily)),
            "avg_count_per_date": as_float(daily["count"].mean()) if not daily.empty else None,
            "min_count_per_date": as_float(daily["count"].min()) if not daily.empty else None,
            "near_zero_std_dates": int((daily["std"].fillna(0).abs() < 1e-12).sum()) if not daily.empty else 0,
            "ret_std": series_distribution(daily["std"]) if not daily.empty else {},
        },
    }


def analyze_label_shift(df: pd.DataFrame) -> dict:
    required = {"code", "date", "close", "future_5d_ret"}
    if not required.issubset(df.columns):
        return {"checked": False, "reason": f"missing columns: {sorted(required - set(df.columns))}"}

    parts = []
    tail_non_null = 0
    for _, g in df.copy().sort_values(["code", "date"]).groupby("code"):
        g = g.copy()
        expected = g["close"].shift(-PRED_HORIZON) / g["close"] - 1
        part = pd.DataFrame(
            {
                "expected": expected,
                "actual": pd.to_numeric(g["future_5d_ret"], errors="coerce"),
            }
        )
        parts.append(part)
        tail_non_null += int(g.tail(PRED_HORIZON)["future_5d_ret"].notna().sum())

    check = pd.concat(parts, ignore_index=True)
    valid = check.dropna()
    diff = (valid["expected"] - valid["actual"]).abs()
    mismatch_threshold = 1e-6
    return {
        "checked": True,
        "comparison_rows": int(len(valid)),
        "max_abs_diff": as_float(diff.max()) if not diff.empty else None,
        "mean_abs_diff": as_float(diff.mean()) if not diff.empty else None,
        "mismatch_threshold": mismatch_threshold,
        "mismatch_rows_gt_threshold": int((diff > mismatch_threshold).sum()) if not diff.empty else 0,
        "last_horizon_non_null_labels": int(tail_non_null),
        "horizon": int(PRED_HORIZON),
    }


def analyze_features(df: pd.DataFrame) -> tuple[list[str], dict]:
    feature_cols = get_alpha158_feature_cols(df)
    news_cols = [c for c in get_news_event_feature_cols() if c in feature_cols]
    alpha_cols = [c for c in feature_cols if c not in set(news_cols)]

    feature_data = df[feature_cols].apply(pd.to_numeric, errors="coerce")
    arr = feature_data.to_numpy(dtype=float, copy=True)
    inf_mask = np.isinf(arr)
    nan_mask = np.isnan(arr)

    missing_rate = feature_data.replace([np.inf, -np.inf], np.nan).isna().mean().sort_values(ascending=False)
    std = feature_data.replace([np.inf, -np.inf], np.nan).std(skipna=True)
    nunique = feature_data.replace([np.inf, -np.inf], np.nan).nunique(dropna=True)
    near_zero = sorted(set(std[std.fillna(0).abs() <= 1e-10].index) | set(nunique[nunique <= 1].index))

    label_like_in_features = [
        c for c in feature_cols
        if c.startswith("future_") or c in {MODEL_REG_LABEL_COL, "ret_5", "ret_20", "vol_20", "drawdown_20"}
    ]

    return feature_cols, {
        "total_model_feature_count": int(len(feature_cols)),
        "alpha158_feature_count": int(len(alpha_cols)),
        "news_feature_count": int(len(news_cols)),
        "top_missing_rate": [
            {"feature": str(k), "missing_rate": as_float(v)}
            for k, v in missing_rate.head(20).items()
        ],
        "near_zero_variance_count": int(len(near_zero)),
        "near_zero_variance_features": near_zero[:50],
        "nan_count": int(nan_mask.sum()),
        "inf_count": int(inf_mask.sum()),
        "feature_cells": int(arr.size),
        "label_like_columns_in_features": label_like_in_features,
    }


def analyze_model_ready_data(df: pd.DataFrame, feature_cols: list[str]) -> dict:
    model_df = prepare_model_data(df, feature_cols)
    if model_df.empty:
        return {"rows": 0}

    dates = sorted(pd.to_datetime(model_df["date"]).unique())
    split_idx = int(len(dates) * 0.8)
    split_date = dates[split_idx] if dates else None
    train_df = model_df[pd.to_datetime(model_df["date"]) < split_date].copy()
    test_df = model_df[pd.to_datetime(model_df["date"]) >= split_date].copy()

    inner_train = pd.DataFrame()
    valid_df = pd.DataFrame()
    if train_df["date"].nunique() >= 50:
        train_dates = sorted(pd.to_datetime(train_df["date"]).unique())
        valid_split_idx = int(len(train_dates) * 0.8)
        valid_split_date = train_dates[valid_split_idx]
        inner_train = train_df[pd.to_datetime(train_df["date"]) < valid_split_date].copy()
        valid_df = train_df[pd.to_datetime(train_df["date"]) >= valid_split_date].copy()

    return {
        "rows": int(len(model_df)),
        "dates": int(len(dates)),
        "date_min": model_df["date"].min(),
        "date_max": model_df["date"].max(),
        "time_split": True,
        "split_date": split_date,
        "train_rows": int(len(train_df)),
        "train_dates": int(train_df["date"].nunique()),
        "test_rows": int(len(test_df)),
        "test_dates": int(test_df["date"].nunique()),
        "test_start": test_df["date"].min() if not test_df.empty else None,
        "test_end": test_df["date"].max() if not test_df.empty else None,
        "inner_train_rows": int(len(inner_train)) if not inner_train.empty else None,
        "inner_train_dates": int(inner_train["date"].nunique()) if not inner_train.empty else None,
        "valid_rows": int(len(valid_df)) if not valid_df.empty else None,
        "valid_dates": int(valid_df["date"].nunique()) if not valid_df.empty else None,
    }


def analyze_prediction_file(pred_df: pd.DataFrame | None, name: str) -> dict:
    if pred_df is None or pred_df.empty:
        return {"available": False}

    data = pred_df.copy()
    if "date" in data.columns:
        data["date"] = pd.to_datetime(data["date"])
    pred_col = MODEL_PRED_COL if MODEL_PRED_COL in data.columns else "pred_5d_ret" if "pred_5d_ret" in data.columns else "raw_score"

    out = {
        "available": True,
        "name": name,
        "rows": int(len(data)),
        "date_min": data["date"].min() if "date" in data.columns else None,
        "date_max": data["date"].max() if "date" in data.columns else None,
        "date_count": int(data["date"].nunique()) if "date" in data.columns else None,
        "stock_count": int(data["code"].astype(str).str.zfill(6).nunique()) if "code" in data.columns else None,
        "prediction_column": pred_col,
        "pred_distribution": series_distribution(data[pred_col]) if pred_col in data.columns else {},
        "up_prob_distribution": series_distribution(data["up_prob"]) if "up_prob" in data.columns else {},
        "score_distribution": series_distribution(data["score"]) if "score" in data.columns else {},
    }

    if "up_prob" in data.columns:
        p = pd.to_numeric(data["up_prob"], errors="coerce").dropna()
        out["up_prob_near_0_5_ratio"] = as_float(((p >= 0.45) & (p <= 0.55)).mean()) if len(p) else None

    target_cols = [c for c in [MODEL_REG_LABEL_COL, "future_5d_ret"] if c in data.columns]
    daily_metric_summary = {}
    topk_summary = {}

    if "date" in data.columns and pred_col in data.columns:
        for target in target_cols:
            ic_values = []
            rankic_values = []
            topk_rows = []
            for date, g in data.groupby("date"):
                g = g.dropna(subset=[pred_col, target]).copy()
                if len(g) < 3:
                    continue
                ic_values.append(calc_ic(g[pred_col], g[target]))
                rankic_values.append(calc_rankic(g[pred_col], g[target]))
                if target == "future_5d_ret":
                    sorted_g = g.sort_values(pred_col, ascending=False)
                    topk_rows.append(
                        {
                            "date": date,
                            "top5": as_float(sorted_g.head(5)[target].mean()),
                            "top10": as_float(sorted_g.head(10)[target].mean()),
                            "top30": as_float(sorted_g.head(30)[target].mean()),
                            "bottom10": as_float(sorted_g.tail(10)[target].mean()),
                        }
                    )

            daily_metric_summary[target] = {
                "ic": summarize_metric(ic_values),
                "rankic": summarize_metric(rankic_values),
            }
            if topk_rows:
                topk_df = pd.DataFrame(topk_rows)
                topk_summary[target] = {
                    "top5_mean_ret": as_float(topk_df["top5"].mean()),
                    "top10_mean_ret": as_float(topk_df["top10"].mean()),
                    "top30_mean_ret": as_float(topk_df["top30"].mean()),
                    "bottom10_mean_ret": as_float(topk_df["bottom10"].mean()),
                    "daily_rows": int(len(topk_df)),
                }

    out["daily_metrics"] = daily_metric_summary
    out["topk_future_return"] = topk_summary
    return out


def build_findings(stats: dict) -> list[str]:
    findings = []
    scope = stats.get("data_scope", {})
    model_ready = stats.get("model_ready_data", {})
    feature = stats.get("feature_quality", {})
    label_shift = stats.get("label_shift_check", {})
    test_pred = stats.get("test_predictions", {})

    if scope.get("trading_days", 0) < 250:
        findings.append(
            f"训练特征日期只有 {scope.get('trading_days')} 个交易日，样本时间明显偏短；这是当前表现不稳的首要风险。"
        )
    if model_ready.get("test_dates") and model_ready.get("test_dates") < 60:
        findings.append(
            f"按当前 80/20 时间切分，测试集只有 {model_ready.get('test_dates')} 个交易日，IC/TopK 评估稳定性不足。"
        )
    if scope.get("stock_count", 0) >= 280:
        findings.append(f"股票池数量为 {scope.get('stock_count')}，CSI300 股票池基本生效。")
    if label_shift.get("checked") and label_shift.get("mismatch_rows_gt_threshold", 1) == 0 and label_shift.get("last_horizon_non_null_labels") == 0:
        findings.append("future_5d_ret 与 close.shift(-5) 校验一致，未发现标签方向或末端标签泄漏。")
    if feature.get("label_like_columns_in_features"):
        findings.append(f"特征列中发现疑似标签/展示列：{feature.get('label_like_columns_in_features')}")
    else:
        findings.append("特征列未包含 future 标签或 ret_5/ret_20/vol_20/drawdown_20 等展示列。")
    if feature.get("near_zero_variance_count", 0) > 0:
        findings.append(f"存在 {feature.get('near_zero_variance_count')} 个近零方差特征，需要后续训练前过滤。")
    if test_pred.get("available"):
        prob_std = test_pred.get("up_prob_distribution", {}).get("std")
        near_prob = test_pred.get("up_prob_near_0_5_ratio")
        if prob_std is not None and prob_std < 0.05:
            findings.append(f"测试集 up_prob 标准差只有 {prob_std:.4f}，分类头区分度偏弱。")
        if near_prob is not None and near_prob > 0.7:
            findings.append(f"测试集 {near_prob:.1%} 的 up_prob 位于 0.45~0.55，概率输出接近随机。")

        ret_metrics = test_pred.get("daily_metrics", {}).get("future_5d_ret", {})
        rankic_mean = ret_metrics.get("rankic", {}).get("mean")
        if rankic_mean is not None and abs(rankic_mean) < 0.02:
            findings.append(f"测试集 pred vs future_5d_ret 的 RankIC 均值为 {rankic_mean:.4f}，排序信号很弱。")

    if not findings:
        findings.append("未发现单一明显故障点，建议进入树模型与特征增强对比。")
    return findings


def fmt_value(value: Any) -> str:
    value = to_builtin(value)
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def table_from_dict(data: dict) -> str:
    lines = ["| 指标 | 数值 |", "|---|---:|"]
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            continue
        lines.append(f"| {key} | {fmt_value(value)} |")
    return "\n".join(lines)


def render_report(stats: dict) -> str:
    findings = stats["findings"]
    scope = stats["data_scope"]
    labels = stats["label_quality"]
    features = stats["feature_quality"]
    model_ready = stats["model_ready_data"]
    test_pred = stats["test_predictions"]
    latest = stats["latest_ranking"]

    top_missing = "\n".join(
        f"- `{x['feature']}`: {fmt_value(x['missing_rate'])}"
        for x in features.get("top_missing_rate", [])
    )
    if not top_missing:
        top_missing = "- N/A"

    report = f"""# 模型表现诊断报告

生成时间：{stats["generated_at"]}

## 结论摘要
{chr(10).join(f"- {item}" for item in findings)}

## 数据范围
{table_from_dict(scope)}

每只股票交易日数量：

```json
{json.dumps(to_builtin(scope.get("stock_trading_days", {})), ensure_ascii=False, indent=2)}
```

## 标签检查

future_5d_ret 分布：

```json
{json.dumps(to_builtin(labels.get("future_5d_ret", {})), ensure_ascii=False, indent=2)}
```

future_5d_up 比例：

```json
{json.dumps(to_builtin(labels.get("future_5d_up", {})), ensure_ascii=False, indent=2)}
```

每日横截面标签有效性：

```json
{json.dumps(to_builtin(labels.get("daily_cross_section", {})), ensure_ascii=False, indent=2)}
```

标签移位/穿越校验：

```json
{json.dumps(to_builtin(stats.get("label_shift_check", {})), ensure_ascii=False, indent=2)}
```

## 特征检查

{table_from_dict(features)}

缺失率最高的前 20 个特征：

{top_missing}

近零方差特征前 50 个：

```json
{json.dumps(to_builtin(features.get("near_zero_variance_features", [])), ensure_ascii=False, indent=2)}
```

## 训练/验证/测试切分

当前训练入口使用时间切分：先按日期 80/20 分 train/test，再在 train 内按日期 80/20 分 inner_train/valid。

```json
{json.dumps(to_builtin(model_ready), ensure_ascii=False, indent=2)}
```

## 测试集预测检查

文件：`{TEST_PREDICTIONS_PATH}`

```json
{json.dumps(to_builtin(test_pred), ensure_ascii=False, indent=2)}
```

## 最新 ranking 检查

文件：`{RANKING_LATEST_PATH}`

```json
{json.dumps(to_builtin(latest), ensure_ascii=False, indent=2)}
```

## 诊断判断

1. 如果数据只有百余个有效交易日，优先补充更长历史数据；复杂模型无法弥补样本时间太短的问题。
2. 如果测试集交易日少于 60 天，IC/RankIC/TopK 收益只能作为烟雾测试，不适合作为稳定模型结论。
3. 当前阶段下一步适合加入 LightGBM，并用同一时间切分对比 MLP、LightGBM 和 External DFT_UNET。

免责声明：本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。
"""
    return report


def main() -> None:
    ensure_dirs()
    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

    feature_df = training_alpha158()
    if feature_df is None or feature_df.empty:
        raise RuntimeError("训练原始数据无法生成 Alpha158 特征")

    feature_df["date"] = pd.to_datetime(feature_df["date"])
    feature_df["code"] = feature_df["code"].astype(str).str.zfill(6)

    feature_cols, feature_quality = analyze_features(feature_df)

    latest_ranking_df = read_csv_if_exists(RANKING_LATEST_PATH)
    test_pred_df = read_csv_if_exists(TEST_PREDICTIONS_PATH)
    eval_df = read_csv_if_exists(EVAL_METRICS_PATH)

    stock_pool = get_stock_pool(token=None, enrich_name=False)

    stats = {
        "generated_at": generated_at,
        "paths": {
            "train_feature_data": "on_demand_from_raw",
            "test_predictions": TEST_PREDICTIONS_PATH,
            "latest_ranking": RANKING_LATEST_PATH,
            "evaluation_metrics": EVAL_METRICS_PATH,
        },
        "universe": {
            "stock_pool_count": int(len(stock_pool)),
        },
        "data_scope": analyze_data_scope(feature_df),
        "label_quality": analyze_labels(feature_df),
        "label_shift_check": analyze_label_shift(feature_df),
        "feature_quality": feature_quality,
        "model_ready_data": analyze_model_ready_data(feature_df, feature_cols),
        "test_predictions": analyze_prediction_file(test_pred_df, "test_predictions"),
        "latest_ranking": analyze_prediction_file(latest_ranking_df, "ranking_latest"),
        "evaluation_metrics_rows": int(len(eval_df)) if eval_df is not None else 0,
    }
    stats["findings"] = build_findings(stats)

    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_PATH.write_text(
        json.dumps(to_builtin(stats), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    REPORT_PATH.write_text(render_report(stats), encoding="utf-8")

    print("=" * 80)
    print("[Model Diagnosis Finished]")
    print(f"[Report] {REPORT_PATH}")
    print(f"[Stats]  {STATS_PATH}")
    print("[Findings]")
    for item in stats["findings"]:
        print(f"- {item}")
    print("=" * 80)


if __name__ == "__main__":
    main()
