from __future__ import annotations

"""Export local model predictions as target weights for a MyQuant/GM backtest.

The GM strategy does not run the local model. It only reads the exported CSV,
then uses MyQuant historical prices, trading calendar, commission, slippage and
matching to backtest the model's daily 10:00 rebalance rule.
"""

import argparse
from pathlib import Path
import sys
from typing import Any

import pandas as pd


def _norm_code(value: Any) -> str:
    text = str(value or "").strip().split(".")[-1].zfill(6)
    return text if text.isdigit() else ""


def _norm_date(value: Any) -> str:
    return str(value or "")[:10]


def _gm_symbol(code: str) -> str:
    code = _norm_code(code)
    if not code:
        return ""
    if code.startswith(("6", "9")):
        return f"SHSE.{code}"
    if code.startswith(("0", "2", "3", "7")):
        return f"SZSE.{code}"
    if code.startswith(("4", "8")):
        return f"BJSE.{code}"
    return ""


def _read_csv(path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"cannot_read_csv_with_common_encodings:{path}")


def _load_predictions(args: argparse.Namespace) -> pd.DataFrame:
    if args.from_db:
        from database.repositories.prediction_repository import PredictionRepository

        rows = PredictionRepository().list_predictions(
            trade_date=args.trade_date,
            limit=None,
        )
        df = pd.DataFrame(rows)
    else:
        if not args.input:
            raise SystemExit("input is required unless --from-db is used")
        df = _read_csv(Path(args.input))

    if df.empty:
        return pd.DataFrame()

    # This covers both the repository payload and the project's
    # outputs/ranking_*.csv schema.
    column_aliases = {
        "signal_date": ["signal_date", "trade_date", "date"],
        "execution_hint_date": [
            "execution_hint_date",
            "prediction_for_date",
            "predict_for_date",
            "prediction_date",
            "next_trade_date",
        ],
        "code": ["code", "stock_code", "ts_code"],
        "name": ["name", "stock_name"],
        "rank": ["pred_rank", "rank"],
        "score": ["pred_score", "score", "raw_score", "model_score"],
        "model_name": ["model_name", "model_version"],
        "risk_score": ["risk_score"],
        "confidence_score": ["confidence_score", "up_prob_calibrated", "up_prob"],
    }
    out = pd.DataFrame()
    for target, aliases in column_aliases.items():
        source = next((col for col in aliases if col in df.columns), None)
        out[target] = df[source] if source else None

    for col in ("risk_score", "confidence_score", "score"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["rank"] = pd.to_numeric(out["rank"], errors="coerce").astype("Int64")

    out["signal_date"] = out["signal_date"].map(_norm_date)
    out["execution_hint_date"] = out["execution_hint_date"].map(_norm_date)
    out["code"] = out["code"].map(_norm_code)
    out["symbol"] = out["code"].map(_gm_symbol)
    out = out[(out["signal_date"] != "") & (out["symbol"] != "")].copy()

    if args.model_name:
        wanted = args.model_name.casefold()
        out = out[out["model_name"].astype(str).str.casefold() == wanted]
    if args.trade_date:
        out = out[out["signal_date"] == args.trade_date]
    if args.start_date:
        out = out[out["signal_date"] >= args.start_date]
    if args.end_date:
        out = out[out["signal_date"] <= args.end_date]
    if args.min_score is not None:
        out = out[out["score"] >= args.min_score]
    if args.min_confidence is not None:
        out["confidence_score"] = out["confidence_score"].fillna(args.min_confidence)
        out = out[out["confidence_score"] >= args.min_confidence]
    if args.max_risk is not None:
        out["risk_score"] = out["risk_score"].fillna(0.0)
        out = out[out["risk_score"] <= args.max_risk]

    return out.sort_values(
        ["signal_date", "rank", "score", "symbol"],
        ascending=[True, True, False, True],
        na_position="last",
    ).reset_index(drop=True)


def export_targets(args: argparse.Namespace) -> tuple[Path, pd.DataFrame]:
    source = _load_predictions(args)
    return export_targets_from_frame(
        source,
        top_k=args.top_k,
        cash_ratio=args.cash_ratio,
        trade_time=args.trade_time,
        output=args.output,
    )


def export_targets_from_frame(
    source: pd.DataFrame,
    *,
    top_k: int,
    cash_ratio: float,
    trade_time: str,
    output: str | Path,
) -> tuple[Path, pd.DataFrame]:
    if source.empty:
        raise RuntimeError("no_valid_predictions_after_filter")

    canonical = pd.DataFrame(index=source.index)
    aliases = {
        "signal_date": ["signal_date", "trade_date", "date"],
        "execution_hint_date": [
            "execution_hint_date",
            "prediction_for_date",
            "predict_for_date",
            "prediction_date",
        ],
        "symbol": ["symbol"],
        "code": ["code", "stock_code", "ts_code"],
        "name": ["name", "stock_name"],
        "rank": ["pred_rank", "rank"],
        "score": ["pred_score", "score", "raw_score", "model_score"],
        "model_name": ["model_name", "model_version"],
    }
    for target, candidates in aliases.items():
        source_column = next((column for column in candidates if column in source.columns), None)
        canonical[target] = source[source_column] if source_column else None
    source = canonical
    source["execution_hint_date"] = source["execution_hint_date"].fillna("")
    source["model_name"] = source["model_name"].fillna("unknown")
    source["rank"] = pd.to_numeric(source["rank"], errors="coerce")
    source["score"] = pd.to_numeric(source["score"], errors="coerce")

    selected_rows: list[dict[str, Any]] = []
    for signal_date, group in source.groupby("signal_date", sort=True):
        # Keep the best row if the same symbol appears more than once.
        group = group.drop_duplicates("symbol", keep="first")
        selected = group.head(max(1, int(top_k))).copy()
        selected["target_weight"] = float(cash_ratio) / len(selected)
        selected_rows.extend(selected.to_dict(orient="records"))

    result = pd.DataFrame(selected_rows)
    result["trade_time"] = trade_time
    result = result[
        [
            "signal_date",
            "trade_time",
            "symbol",
            "code",
            "name",
            "rank",
            "score",
            "target_weight",
            "model_name",
            "execution_hint_date",
        ]
    ]
    result = result.sort_values(["signal_date", "rank", "symbol"]).reset_index(drop=True)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False, encoding="utf-8-sig")
    return output, result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="model ranking/prediction CSV")
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="read the model_prediction table instead of a CSV; needs DB env vars",
    )
    parser.add_argument(
        "--trade-date",
        help="only export one signal date, e.g. 2026-09-18",
    )
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--model-name")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--cash-ratio",
        type=float,
        default=0.98,
        help="sum of target weights; keep a little cash for commission/slippage",
    )
    parser.add_argument("--min-score", type=float)
    parser.add_argument("--min-confidence", type=float)
    parser.add_argument("--max-risk", type=float)
    parser.add_argument(
        "--trade-time",
        default="10:00:00",
        help="informational execution time written into the CSV",
    )
    parser.add_argument(
        "--output",
        default="outputs/gm/gm_daily_1000_targets.csv",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output, result = export_targets(args)
    dates = result["signal_date"].nunique()
    print(f"exported {len(result)} target rows / {dates} signal dates -> {output}")
    print("first rows:")
    print(result.head(min(10, len(result))).to_string(index=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"export_failed: {exc}", file=sys.stderr)
        raise
