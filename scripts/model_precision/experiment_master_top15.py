from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import torch
from torch import nn


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_ranker import MasterRanker


DEFAULT_FEATURE_CACHE = (
    ROOT / "data" / "model_precision" / "stock_direction_features_v5_alpha.parquet"
)
DEFAULT_DAILY_BASIC = ROOT / "data" / "model_precision" / "tushare" / "daily_basic.csv"
DEFAULT_REPORT = ROOT / "outputs" / "model_precision" / "master_no_kronos_top15.json"
DEFAULT_PREDICTIONS = (
    ROOT / "data" / "model_precision" / "master_no_kronos_top15.parquet"
)
DEFAULT_MODEL_DIR = ROOT / "models" / "cross_sectional_master" / "experiments"
DISCLAIMER = "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。"

FORBIDDEN_FEATURE_TERMS = (
    "kronos",
    "pred_return",
    "up_prob",
    "existing_rank",
    "direction_hit",
    "direction_accuracy",
    "positive_correct",
    "positive_precision",
)


@dataclass(frozen=True)
class ExperimentConfig:
    sequence_length: int
    model_dimension: int
    temporal_heads: int
    stock_heads: int
    dropout: float
    gate_temperature: float
    learning_rate: float
    weight_decay: float
    epochs: int
    seed: int
    max_train_stocks: int
    loss: str


def _atomic_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def select_feature_columns(schema_columns: Iterable[str]) -> tuple[list[str], list[str]]:
    """Return stock and market features with no Kronos prediction lineage."""

    stock: list[str] = []
    market: list[str] = []
    for column in schema_columns:
        lowered = column.lower()
        if any(term in lowered for term in FORBIDDEN_FEATURE_TERMS):
            continue
        is_stock = column.startswith(("basic_", "flow_", "margin_"))
        is_stock = is_stock or column == "relative_net_flow"
        is_stock = is_stock or column.startswith(
            (
                "cross_pct_flow_",
                "cross_pct_margin_",
            )
        )
        is_market = column.startswith(
            ("market_index_", "market_hsgt_", "market_prior_up_rate_")
        )
        is_market = is_market or column in {
            "market_current_net_flow_mean",
            "market_current_institutional_flow_mean",
            "market_current_turnover_mean",
        }
        if is_stock:
            stock.append(column)
        elif is_market:
            market.append(column)
    return sorted(set(stock)), sorted(set(market))


def load_tushare_labels(path: Path) -> pd.DataFrame:
    labels = pd.read_csv(
        path,
        dtype={"ts_code": str, "trade_date": str},
        usecols=["ts_code", "trade_date", "close"],
    )
    labels["code"] = labels["ts_code"].str.split(".").str[0].str.zfill(6)
    labels["date"] = pd.to_datetime(
        labels["trade_date"], format="%Y%m%d", errors="coerce"
    )
    labels["close"] = pd.to_numeric(labels["close"], errors="coerce")
    labels = labels.dropna(subset=["date", "code", "close"]).sort_values(
        ["code", "date"], kind="stable"
    )
    next_close = labels.groupby("code", sort=False)["close"].shift(-1)
    labels["future_1d_ret_tushare"] = next_close / labels["close"] - 1.0
    labels["label_up_tushare"] = labels["future_1d_ret_tushare"].gt(0.0).astype(
        "float32"
    ).where(next_close.notna())
    return labels.loc[
        :, ["date", "code", "future_1d_ret_tushare", "label_up_tushare"]
    ]


def robust_normalize(
    values: np.ndarray,
    training_mask: np.ndarray,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    training = values[training_mask]
    median = np.nanmedian(training, axis=0)
    absolute_deviation = np.nanmedian(np.abs(training - median), axis=0)
    scale = absolute_deviation * 1.4826
    fallback = np.nanstd(training, axis=0)
    scale = np.where(np.isfinite(scale) & (scale > 1e-8), scale, fallback)
    scale = np.where(np.isfinite(scale) & (scale > 1e-8), scale, 1.0)
    median = np.where(np.isfinite(median), median, 0.0)
    normalized = (values - median) / scale
    normalized = np.clip(normalized, -3.0, 3.0)
    normalized = np.nan_to_num(normalized, nan=0.0, posinf=3.0, neginf=-3.0)
    return normalized.astype("float32"), {"median": median, "scale": scale}


class DailySequencePanel:
    def __init__(
        self,
        frame: pd.DataFrame,
        features: np.ndarray,
        *,
        sequence_length: int,
    ) -> None:
        self.frame = frame.reset_index(drop=True)
        self.features = features
        self.sequence_length = int(sequence_length)
        self.history = np.full(
            (len(frame), self.sequence_length), -1, dtype="int32"
        )
        for _, positions in self.frame.groupby("code", sort=False).indices.items():
            ordered = np.asarray(positions, dtype="int32")
            if len(ordered) < self.sequence_length:
                continue
            windows = np.lib.stride_tricks.sliding_window_view(
                ordered, self.sequence_length
            )
            self.history[ordered[self.sequence_length - 1 :]] = windows
        valid = self.history[:, 0] >= 0
        self.rows_by_date = {
            pd.Timestamp(date): np.asarray(rows, dtype="int32")[
                valid[np.asarray(rows, dtype="int32")]
            ]
            for date, rows in self.frame.groupby("date", sort=True).indices.items()
        }

    def dates_between(self, start: str, end: str) -> list[pd.Timestamp]:
        lower, upper = pd.Timestamp(start), pd.Timestamp(end)
        return [date for date in self.rows_by_date if lower <= date <= upper]

    def batch(
        self,
        date: pd.Timestamp,
        *,
        maximum_stocks: int = 0,
        rng: np.random.Generator | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, np.ndarray]:
        rows = self.rows_by_date[date]
        label_values = self.frame.loc[rows, "future_1d_ret_tushare"].to_numpy(
            dtype="float32"
        )
        usable = np.isfinite(label_values)
        rows = rows[usable]
        label_values = label_values[usable]
        if maximum_stocks and len(rows) > maximum_stocks:
            if rng is None:
                raise ValueError("rng is required when sampling stocks")
            selected = np.sort(rng.choice(len(rows), maximum_stocks, replace=False))
            rows = rows[selected]
            label_values = label_values[selected]
        labels_up = self.frame.loc[rows, "label_up_tushare"].to_numpy(
            dtype="float32", copy=True
        )
        sequences = self.features[self.history[rows]]
        return (
            torch.from_numpy(sequences),
            torch.from_numpy(label_values),
            torch.from_numpy(labels_up),
            rows,
        )


def _cross_sectional_return_target(returns: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    order = torch.argsort(returns)
    trim = int(len(order) * 0.025)
    mask = torch.ones(len(order), dtype=torch.bool, device=returns.device)
    if trim:
        mask[order[:trim]] = False
        mask[order[-trim:]] = False
    selected = returns[mask]
    target = (selected - selected.mean()) / selected.std().clamp_min(1e-6)
    return mask, target


def _loss(
    scores: torch.Tensor,
    returns: torch.Tensor,
    labels_up: torch.Tensor,
    kind: str,
) -> torch.Tensor:
    mask, normalized_returns = _cross_sectional_return_target(returns)
    mse = torch.mean((scores[mask] - normalized_returns) ** 2)
    if kind == "mse":
        return mse
    binary = nn.functional.binary_cross_entropy_with_logits(scores, labels_up)
    if kind == "bce":
        return binary
    positives = scores[labels_up.gt(0.5)]
    negatives = scores[labels_up.le(0.5)]
    if len(positives) and len(negatives):
        pairwise = nn.functional.softplus(
            -(positives[:, None] - negatives[None, :])
        ).mean()
    else:
        pairwise = binary
    if kind == "pairwise":
        return pairwise
    if kind == "rank_bce":
        return pairwise + 0.25 * binary
    return mse + 0.25 * binary


def top15_metrics(scored: pd.DataFrame) -> dict[str, Any]:
    top = (
        scored.sort_values(
            ["date", "master_score", "code"],
            ascending=[True, False, True],
            kind="stable",
        )
        .groupby("date", sort=False)
        .head(15)
    )
    counts = top.groupby("date", sort=True).size()
    daily = top.groupby("date", sort=True)["label_up_tushare"].mean()
    monthly = top.assign(month=top["date"].dt.to_period("M").astype(str)).groupby(
        "month", sort=True
    )["label_up_tushare"].agg(signals="size", correct="sum", precision="mean")
    return {
        "start_date": str(daily.index.min().date()),
        "end_date": str(daily.index.max().date()),
        "days": int(len(daily)),
        "signals": int(len(top)),
        "correct": int(top["label_up_tushare"].sum()),
        "precision": float(daily.mean()),
        "all_days_have_15": bool(not counts.empty and counts.eq(15).all()),
        "monthly": [
            {
                "month": str(month),
                "signals": int(row["signals"]),
                "correct": int(row["correct"]),
                "precision": float(row["precision"]),
            }
            for month, row in monthly.iterrows()
        ],
    }


def _set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)


def train_model(
    panel: DailySequencePanel,
    *,
    dates: list[pd.Timestamp],
    stock_feature_count: int,
    market_feature_count: int,
    config: ExperimentConfig,
    device: torch.device,
) -> tuple[MasterRanker, list[float]]:
    _set_seed(config.seed)
    model = MasterRanker(
        stock_feature_count=stock_feature_count,
        market_feature_count=market_feature_count,
        model_dimension=config.model_dimension,
        temporal_heads=config.temporal_heads,
        stock_heads=config.stock_heads,
        temporal_dropout=config.dropout,
        stock_dropout=config.dropout,
        gate_temperature=config.gate_temperature,
        maximum_lookback=config.sequence_length,
    ).to(device)
    optimizer = torch.optim.Adam(
        model.parameters(),
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )
    rng = np.random.default_rng(config.seed)
    history: list[float] = []
    for epoch in range(1, config.epochs + 1):
        model.train()
        shuffled = list(dates)
        rng.shuffle(shuffled)
        losses: list[float] = []
        for index, date in enumerate(shuffled, start=1):
            features, returns, labels_up, _ = panel.batch(
                date,
                maximum_stocks=config.max_train_stocks,
                rng=rng,
            )
            if len(returns) < 16:
                continue
            features = features.to(device)
            returns = returns.to(device)
            labels_up = labels_up.to(device)
            optimizer.zero_grad(set_to_none=True)
            scores = model(features)
            loss = _loss(scores, returns, labels_up, config.loss)
            loss.backward()
            torch.nn.utils.clip_grad_value_(model.parameters(), 3.0)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
            if index % 200 == 0:
                print(
                    f"epoch={epoch}/{config.epochs} day={index}/{len(shuffled)} "
                    f"loss={np.mean(losses[-200:]):.6f}",
                    flush=True,
                )
        epoch_loss = float(np.mean(losses))
        history.append(epoch_loss)
        print(f"epoch={epoch}/{config.epochs} mean_loss={epoch_loss:.6f}", flush=True)
    return model, history


def predict(
    model: MasterRanker,
    panel: DailySequencePanel,
    dates: list[pd.Timestamp],
    device: torch.device,
) -> pd.DataFrame:
    model.eval()
    parts: list[pd.DataFrame] = []
    with torch.no_grad():
        for index, date in enumerate(dates, start=1):
            features, _, _, rows = panel.batch(date)
            if len(rows) < 15:
                continue
            scores = model(features.to(device)).detach().cpu().numpy()
            part = panel.frame.loc[
                rows,
                ["date", "code", "future_1d_ret_tushare", "label_up_tushare"],
            ].copy()
            part["master_score"] = scores.astype("float32")
            parts.append(part)
            if index % 50 == 0:
                print(f"predict day={index}/{len(dates)}", flush=True)
    return pd.concat(parts, ignore_index=True)


def _prepare_panel(
    args: argparse.Namespace,
    training_end: str,
    feature_columns: tuple[list[str], list[str]] | None = None,
) -> tuple[DailySequencePanel, list[str], list[str], dict[str, Any]]:
    schema = pq.ParquetFile(args.feature_cache).schema.names
    selected_stock, selected_market = feature_columns or select_feature_columns(schema)
    requested = ["date", "code", "future_1d_ret", "label_up", *selected_stock, *selected_market]
    frame = pd.read_parquet(args.feature_cache, columns=requested)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["code"] = frame["code"].astype(str).str.zfill(6)
    labels = load_tushare_labels(args.daily_basic)
    frame = frame.drop(columns=["future_1d_ret", "label_up"]).merge(
        labels, on=["date", "code"], how="left", validate="one_to_one"
    )
    frame = frame.sort_values(["date", "code"], kind="stable").reset_index(drop=True)
    training_mask = frame["date"].le(training_end).to_numpy()
    usable_stock = [
        column
        for column in selected_stock
        if frame.loc[training_mask, column].notna().mean() >= 0.80
        and frame.loc[training_mask, column].std(skipna=True) > 1e-8
    ]
    usable_market = [
        column
        for column in selected_market
        if frame.loc[training_mask, column].notna().mean() >= 0.80
        and frame.loc[training_mask, column].std(skipna=True) > 1e-8
    ]
    ordered = [*usable_stock, *usable_market]
    raw = frame.loc[:, ordered].to_numpy(dtype="float32")
    normalized, normalization = robust_normalize(raw, training_mask)
    panel = DailySequencePanel(
        frame,
        normalized,
        sequence_length=args.sequence_length,
    )
    agreement = frame.loc[
        frame["label_up_tushare"].notna(), ["date", "code", "label_up_tushare"]
    ].copy()
    legacy = pd.read_parquet(
        args.feature_cache, columns=["date", "code", "label_up"]
    )
    legacy["date"] = pd.to_datetime(legacy["date"], errors="coerce").dt.normalize()
    legacy["code"] = legacy["code"].astype(str).str.zfill(6)
    agreement = agreement.merge(legacy, on=["date", "code"], how="left")
    compared = agreement.dropna(subset=["label_up"])
    audit = {
        "rows": int(len(frame)),
        "stocks": int(frame["code"].nunique()),
        "days": int(frame["date"].nunique()),
        "training_end": training_end,
        "tushare_vs_legacy_label_agreement": float(
            compared["label_up_tushare"].eq(compared["label_up"]).mean()
        ),
        "normalization_median_finite": bool(np.isfinite(normalization["median"]).all()),
        "normalization_scale_positive": bool((normalization["scale"] > 0).all()),
    }
    return panel, usable_stock, usable_market, audit


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Experiment with MASTER cross-sectional ranking without Kronos inputs."
    )
    parser.add_argument("--feature-cache", type=Path, default=DEFAULT_FEATURE_CACHE)
    parser.add_argument("--daily-basic", type=Path, default=DEFAULT_DAILY_BASIC)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--predictions-path", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--sequence-length", type=int, default=8)
    parser.add_argument("--model-dimension", type=int, default=32)
    parser.add_argument("--temporal-heads", type=int, default=4)
    parser.add_argument("--stock-heads", type=int, default=4)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--gate-temperature", type=float, default=5.0)
    parser.add_argument("--learning-rate", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-5)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--max-train-stocks", type=int, default=256)
    parser.add_argument(
        "--loss",
        choices=("mse", "bce", "hybrid", "pairwise", "rank_bce"),
        default="mse",
    )
    parser.add_argument("--train-start", default="2018-02-14")
    parser.add_argument("--target-precision", type=float, default=0.55)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = ExperimentConfig(
        sequence_length=args.sequence_length,
        model_dimension=args.model_dimension,
        temporal_heads=args.temporal_heads,
        stock_heads=args.stock_heads,
        dropout=args.dropout,
        gate_temperature=args.gate_temperature,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        epochs=args.epochs,
        seed=args.seed,
        max_train_stocks=args.max_train_stocks,
        loss=args.loss,
    )
    torch.set_num_threads(max(1, min(8, os.cpu_count() or 1)))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device={device} config={asdict(config)}", flush=True)

    panel_2025, stock_columns, market_columns, audit_2025 = _prepare_panel(
        args, "2024-12-31"
    )
    train_2025_dates = panel_2025.dates_between(args.train_start, "2024-12-31")
    score_2025_dates = panel_2025.dates_between("2025-01-01", "2025-12-31")
    print(
        f"2025 fit: stock_features={len(stock_columns)} market_features={len(market_columns)} "
        f"train_days={len(train_2025_dates)} score_days={len(score_2025_dates)}",
        flush=True,
    )
    model_2025, loss_2025 = train_model(
        panel_2025,
        dates=train_2025_dates,
        stock_feature_count=len(stock_columns),
        market_feature_count=len(market_columns),
        config=config,
        device=device,
    )
    predictions_2025 = predict(model_2025, panel_2025, score_2025_dates, device)
    metrics_2025 = top15_metrics(predictions_2025)
    print(f"2025 metrics={metrics_2025}", flush=True)

    panel_2026, stock_columns_2026, market_columns_2026, audit_2026 = _prepare_panel(
        args,
        "2025-12-31",
        (stock_columns, market_columns),
    )
    if stock_columns_2026 != stock_columns or market_columns_2026 != market_columns:
        raise AssertionError("Feature contract changed between annual fits")
    train_2026_dates = panel_2026.dates_between(args.train_start, "2025-12-31")
    score_2026_dates = panel_2026.dates_between("2026-01-01", "2026-12-31")
    del model_2025, panel_2025
    model_2026, loss_2026 = train_model(
        panel_2026,
        dates=train_2026_dates,
        stock_feature_count=len(stock_columns),
        market_feature_count=len(market_columns),
        config=config,
        device=device,
    )
    predictions_2026 = predict(model_2026, panel_2026, score_2026_dates, device)
    metrics_2026 = top15_metrics(predictions_2026)
    print(f"2026 metrics={metrics_2026}", flush=True)

    predictions = pd.concat([predictions_2025, predictions_2026], ignore_index=True)
    args.predictions_path.parent.mkdir(parents=True, exist_ok=True)
    predictions.to_parquet(args.predictions_path, index=False)
    args.model_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.model_dir / f"master_no_kronos_{args.loss}_through_2025.pt"
    torch.save(
        {
            "state_dict": copy.deepcopy(model_2026.state_dict()),
            "stock_features": stock_columns,
            "market_features": market_columns,
            "config": asdict(config),
            "trained_through": "2025-12-31",
            "research_only": True,
        },
        checkpoint,
    )
    target = float(args.target_precision)
    accepted = bool(
        metrics_2025["all_days_have_15"]
        and metrics_2026["all_days_have_15"]
        and metrics_2025["precision"] >= target
        and metrics_2026["precision"] >= target
    )
    forbidden_used = [
        column
        for column in [*stock_columns, *market_columns]
        if any(term in column.lower() for term in FORBIDDEN_FEATURE_TERMS)
    ]
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "accepted": accepted,
        "objective": "no_kronos_master_fixed_daily_top15_next_day_up_precision",
        "target_precision": target,
        "selection_count_per_day": 15,
        "model": {
            "name": "MASTER",
            "source": "https://github.com/SJTU-DMTai/MASTER",
            "source_commit": "de8f58557096abde4216a701b35fc4368158d111",
            "license": "MIT",
            "architecture": "market gate + temporal attention + cross-stock attention + temporal aggregation",
            "config": asdict(config),
            "checkpoint": str(checkpoint),
            "research_only": True,
        },
        "features": {
            "stock_count": len(stock_columns),
            "market_count": len(market_columns),
            "stock": stock_columns,
            "market": market_columns,
            "forbidden_kronos_lineage_features": forbidden_used,
        },
        "data_contract": {
            "feature_source": "Tushare-derived cached fundamentals, money flow, margin and index state",
            "label_source": str(args.daily_basic),
            "label": "exact next available trading-day Tushare close > current close",
            "kronos_model_used": False,
            "kronos_predictions_used_as_features": False,
            "legacy_prediction_file_used_only_as_historical_universe_keys": True,
            "uses_future_features": False,
            "audit_2025": audit_2025,
            "audit_2026": audit_2026,
        },
        "training": {
            "2025_model_train_end": "2024-12-31",
            "2026_model_train_end": "2025-12-31",
            "loss_history_2025_model": loss_2025,
            "loss_history_2026_model": loss_2026,
        },
        "validation_2025": metrics_2025,
        "holdout_2026": metrics_2026,
        "promotion_decision": "not_evaluated" if accepted else "rejected",
        "disclaimer": DISCLAIMER,
    }
    _atomic_json(report, args.report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
