from __future__ import annotations

import csv

from database.runtime_data_import import import_ranking_file
from pipelines.prediction_pipeline import run_prediction_pipeline
from pipelines.schemas import PipelineContext, PipelineStatus
from ranking_runtime.settings import ACTIVE_MODEL_NAME


def test_prediction_pipeline_missing_ranking_does_not_crash(tmp_path) -> None:
    context = PipelineContext(output_dir=tmp_path / "outputs", db_path=tmp_path / "agent_quant.db")

    result = run_prediction_pipeline(context)

    assert result.status == PipelineStatus.FAILED
    assert result.errors
    assert result.predictions == []


def test_prediction_pipeline_reads_database_ranking(tmp_path) -> None:
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    path = output_dir / "ranking_latest.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["date", "code", "name", "score", "rank", "confidence", "risk_level", "industry", "model_name"])
        writer.writeheader()
        writer.writerow({"date": "2026-06-11", "code": "000001", "name": "Demo", "score": "0.9", "rank": "1", "confidence": "high", "risk_level": "medium", "industry": "bank", "model_name": ACTIVE_MODEL_NAME})

    db_path = tmp_path / "agent_quant.db"
    import_ranking_file(path, db_path=db_path)
    result = run_prediction_pipeline(
        PipelineContext(output_dir=output_dir, db_path=db_path, top_k=10)
    )

    assert result.status == PipelineStatus.SUCCESS
    assert result.source == "database.model_prediction"
    assert result.predictions[0].stock_code == "000001"
    assert result.predictions[0].pred_score == 0.9
