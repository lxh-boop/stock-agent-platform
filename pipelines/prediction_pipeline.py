from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from database.repositories import PredictionRepository
from pipelines.schemas import PipelineContext, PipelineStatus, PredictionPipelineResult
from scoring.schemas import ModelPredictionSignal
from ranking_runtime.settings import ACTIVE_MODEL_NAME


def _default_ranking_path(context: PipelineContext) -> Path:
    return context.resolved_output_dir() / "ranking_latest.csv"


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def _to_prediction(row: dict[str, Any], total_count: int) -> ModelPredictionSignal:
    data = dict(row)
    data.setdefault("total_count", total_count)
    return ModelPredictionSignal.from_mapping(data)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _from_database(context: PipelineContext) -> list[dict[str, Any]]:
    repo = PredictionRepository(context.db_path)
    trade_date = None if context.trade_date == "latest" else context.trade_date
    if trade_date is None:
        rows = repo.list_latest_predictions(model_name=ACTIVE_MODEL_NAME)
    else:
        rows = repo.list_predictions(trade_date=trade_date)
        rows = [
            row
            for row in rows
            if str(row.get("model_name") or "").strip() == ACTIVE_MODEL_NAME
        ]
    rows.sort(key=lambda row: int(row.get("pred_rank") or 999999))
    return rows


def run_prediction_pipeline(
    context: PipelineContext,
    ranking_path: str | Path | None = None,
) -> PredictionPipelineResult:
    path = Path(ranking_path) if ranking_path else _default_ranking_path(context)
    errors: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    source = ""

    if ranking_path is not None:
        if not path.exists():
            return PredictionPipelineResult(
                status=PipelineStatus.FAILED,
                message="Explicit offline ranking file is missing.",
                input_count=0,
                output_count=0,
                errors=[f"missing offline ranking file: {path}"],
                predictions=[],
                source=str(path),
            )
        rows = _read_csv(path)
        source = str(path)
    else:
        try:
            rows = _from_database(context)
            source = "database.model_prediction"
        except Exception as exc:
            errors.append(f"failed to read database.model_prediction: {exc}")

    if not rows:
        return PredictionPipelineResult(
            status=PipelineStatus.FAILED,
            message="No model predictions are available in database.model_prediction.",
            input_count=0,
            output_count=0,
            output_paths={},
            errors=errors or ["database.model_prediction is empty"],
            warnings=warnings,
            predictions=[],
            source=source,
        )

    total_rows = len(rows)
    fixed_top15 = any("top15_up_signal" in row for row in rows)
    if fixed_top15:
        rows = [row for row in rows if _as_bool(row.get("top15_up_signal"))]
        if len(rows) != min(15, total_rows):
            return PredictionPipelineResult(
                status=PipelineStatus.FAILED,
                message="Fixed daily Top15 ranking is incomplete.",
                input_count=total_rows,
                output_count=0,
                errors=[f"expected_top15={min(15, total_rows)}; actual={len(rows)}"],
                predictions=[],
                source=source,
            )

    declared_models = {
        str(row.get("model_name") or "").strip()
        for row in rows
        if str(row.get("model_name") or "").strip()
    }
    if declared_models and declared_models != {ACTIVE_MODEL_NAME}:
        models = ", ".join(sorted(declared_models))
        return PredictionPipelineResult(
            status=PipelineStatus.FAILED,
            message=f"Rejected retired model ranking: {models}",
            input_count=len(rows),
            output_count=0,
            errors=[f"expected_model={ACTIVE_MODEL_NAME}; actual_models={models}"],
            warnings=warnings,
            predictions=[],
            source=source,
        )

    total = len(rows)
    limit = 15 if fixed_top15 else max(1, int(context.top_k))
    predictions = [_to_prediction(row, total) for row in rows[:limit]]
    actual_trade_dates = [item.trade_date for item in predictions if item.trade_date]
    output_paths = {"ranking": str(path)} if ranking_path is not None else {}
    return PredictionPipelineResult(
        status=PipelineStatus.SUCCESS,
        message=f"Loaded {len(predictions)} model predictions.",
        input_count=total_rows,
        output_count=len(predictions),
        output_paths=output_paths,
        warnings=warnings,
        predictions=predictions,
        source=source,
    )
