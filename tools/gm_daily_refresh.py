from __future__ import annotations

"""Run the once-a-day post-close GM data update and single-model inference."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_gm import (
    build_gm_daily_feature_panel,
    fetch_gm_universe_daily_history,
    load_symbols_file,
    select_signal_date,
    set_gm_token,
)
from gm_daily_ranker import GmDailyRanker, MODEL_NAME
from tools.export_gm_targets import export_targets_from_frame


DISCLAIMER = "本项目仅用于模型验证和研究，不构成投资建议。"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)


def _to_prediction_records(
    ranked: pd.DataFrame,
    *,
    signal_date: str,
    model_name: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in ranked.to_dict(orient="records"):
        records.append(
            {
                "trade_date": signal_date,
                "prediction_for_date": "",
                "stock_code": str(row.get("code") or ""),
                "stock_name": str(row.get("name") or ""),
                "model_name": model_name,
                "pred_score": float(row.get("pred_score") or 0.0),
                "pred_rank": int(row.get("pred_rank") or 0),
                "source_kind": "gm_daily_ranking",
                "raw_score": float(row.get("raw_score") or 0.0),
                "data_source": "gm_daily_bars",
            }
        )
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols-file",
        default=str(ROOT / "datacsi300_stock_pool.csv"),
        help="CSV containing code/code, optional symbol and name",
    )
    parser.add_argument("--symbols", help="comma-separated GM symbols or 6-digit A-share codes")
    parser.add_argument("--model-path", default=os.getenv("GM_DAILY_MODEL_PATH", ""))
    parser.add_argument("--trade-date", help="expected T signal date, e.g. 2026-09-21")
    parser.add_argument("--lookback", type=int, default=260)
    parser.add_argument("--min-coverage", type=float, default=0.90)
    parser.add_argument("--request-interval", type=float, default=0.0)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--cash-ratio", type=float, default=0.98)
    parser.add_argument("--dry-run", action="store_true", help="skip PostgreSQL persistence")
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="only fetch and cache GM daily bars; do not run inference",
    )
    parser.add_argument(
        "--targets-output",
        default=str(ROOT / "strategies" / "gm_daily_1000_targets.csv"),
    )
    parser.add_argument(
        "--bars-output",
        default=str(ROOT / "outputs" / "gm" / "gm_bars_latest.csv"),
        help="write the normalized rolling GM daily-bar cache here",
    )
    parser.add_argument(
        "--metrics-output",
        default=str(ROOT / "outputs" / "gm" / "gm_daily_refresh_metrics.json"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    if args.symbols:
        symbols = []
        for item in str(args.symbols).split(","):
            from data_gm import normalize_gm_symbol

            symbol = normalize_gm_symbol(item)
            if symbol:
                symbols.append({"symbol": symbol, "code": symbol.split(".")[-1], "name": ""})
    else:
        symbols = load_symbols_file(args.symbols_file)
    if not symbols:
        raise RuntimeError("no_valid_symbols")

    model_path = str(args.model_path or "").strip()
    if not args.fetch_only:
        if not model_path:
            raise RuntimeError("GM_DAILY_MODEL_PATH or --model-path is required")
        ranker = GmDailyRanker.from_file(model_path)

    set_gm_token(os.getenv("GM_TOKEN", ""))
    # Daily bars are requested through the close time; GM will return the latest
    # completed daily bars available to the token.
    end_time = f"{args.trade_date} 15:00:00" if args.trade_date else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bars, fetch_errors = fetch_gm_universe_daily_history(
        symbols,
        end_time=end_time,
        count=int(args.lookback),
        request_interval_seconds=float(args.request_interval),
    )
    bars_output = Path(args.bars_output)
    _atomic_csv(bars_output, bars)
    signal_date = select_signal_date(
        bars,
        expected_date=args.trade_date,
        min_coverage=float(args.min_coverage),
    )
    if args.fetch_only:
        metrics = {
            "status": "fetch_only",
            "started_at": started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "signal_date": signal_date,
            "data_source": "gm_daily_bars",
            "universe_requested": len(symbols),
            "universe_fetched": int(bars["code"].nunique()),
            "snapshot_rows": 0,
            "fetch_errors": fetch_errors,
            "bars_path": str(bars_output.resolve()),
            "disclaimer": DISCLAIMER,
        }
        _atomic_json(Path(args.metrics_output), metrics)
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    feature_panel = build_gm_daily_feature_panel(bars, signal_date=signal_date)
    ranked = ranker.predict(feature_panel)

    model_full_name = f"{MODEL_NAME}_{ranker.model_version}"
    ranked["model_name"] = model_full_name
    ranked["trade_date"] = signal_date
    ranked["date"] = signal_date
    ranked["signal_date"] = signal_date

    records = _to_prediction_records(ranked, signal_date=signal_date, model_name=model_full_name)
    persisted_count = 0
    if not args.dry_run:
        from database.repositories.prediction_repository import PredictionRepository

        persisted = PredictionRepository().replace_snapshot(
            records,
            source_kind="gm_daily_ranking",
            exclusive_scope=True,
        )
        persisted_count = len(persisted)

    targets_path, targets = export_targets_from_frame(
        ranked,
        top_k=int(args.top_k),
        cash_ratio=float(args.cash_ratio),
        trade_time="10:00:00",
        output=args.targets_output,
    )

    date_key = signal_date.replace("-", "")
    ranking_path = ROOT / "outputs" / "gm" / f"ranking_{date_key}_{model_full_name}.csv"
    latest_path = ROOT / "outputs" / "gm" / "ranking_latest.csv"
    _atomic_csv(ranking_path, ranked)
    _atomic_csv(latest_path, ranked)

    metrics = {
        "status": "success",
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "signal_date": signal_date,
        "data_source": "gm_daily_bars",
        "universe_requested": len(symbols),
        "universe_fetched": int(bars["code"].nunique()),
        "snapshot_rows": int(len(ranked)),
        "fetch_errors": fetch_errors,
        "model_name": model_full_name,
        "model_path": str(Path(model_path).resolve()),
        "database_persisted_rows": persisted_count,
        "dry_run": bool(args.dry_run),
        "targets_path": str(targets_path.resolve()),
        "bars_path": str(bars_output.resolve()),
        "target_rows": int(len(targets)),
        "ranking_path": str(ranking_path.resolve()),
        "latest_ranking_path": str(latest_path.resolve()),
        "disclaimer": DISCLAIMER,
    }
    _atomic_json(Path(args.metrics_output), metrics)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"gm_daily_refresh_failed:{type(exc).__name__}: {exc}", file=sys.stderr)
        raise
