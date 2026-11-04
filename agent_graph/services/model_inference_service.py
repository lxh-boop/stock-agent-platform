from __future__ import annotations

from pathlib import Path
from typing import Any

from database.repositories import PredictionRepository
from ranking_runtime.settings import (
    ACTIVE_MODEL_BACKEND,
    ACTIVE_MODEL_NAME,
    ACTIVE_MODEL_VERSION,
)


def _code(value: Any) -> str:
    text = str(value or "").strip().split(".")[0]
    return text.zfill(6) if text.isdigit() else text


class ModelInferenceService:
    """Read completed real-model inference outputs from database authority.

    Running the active cross-sectional ranker is intentionally not a synchronous service
    method. The existing Task Runtime owns that long-running operation; these
    queries expose only a completed, persisted inference snapshot.
    """

    def _rows(
        self,
        *,
        db_path: str | Path | None,
        stock_code: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return PredictionRepository(db_path).list_latest_predictions(
            stock_code=_code(stock_code) if stock_code else None,
            model_name=ACTIVE_MODEL_NAME,
            limit=limit,
        )

    @staticmethod
    def _envelope(
        records: list[dict[str, Any]],
        *,
        message: str,
    ) -> dict[str, Any]:
        as_of_date = str((records[0] if records else {}).get("trade_date") or "")
        return {
            "success": bool(records),
            "message": message if records else "No completed model inference is available.",
            "data": {
                "records": records,
                "record_count": len(records),
                "as_of_date": as_of_date,
                "model_backend": ACTIVE_MODEL_BACKEND,
                "model_name": ACTIVE_MODEL_NAME,
                "model_version": ACTIVE_MODEL_VERSION,
                "inference_mode": "completed_task_snapshot",
                "source": "database/model_prediction",
                "long_running_execution": "task_runtime",
            },
            "warnings": [] if records else ["completed_inference_unavailable"],
            "errors": [],
        }

    def predict_stock_score(
        self,
        stock_code: str,
        *,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        rows = self._rows(db_path=db_path, stock_code=stock_code, limit=1)
        return self._envelope(rows, message="Completed stock inference queried.")

    def predict_rank(
        self,
        *,
        top_k: int,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        limit = max(1, min(int(top_k or 10), 500))
        rows = self._rows(db_path=db_path, limit=limit)
        return self._envelope(rows, message="Completed model ranking queried.")

    def predict_risk(
        self,
        stock_code: str,
        *,
        db_path: str | Path | None,
    ) -> dict[str, Any]:
        rows = self._rows(db_path=db_path, stock_code=stock_code, limit=1)
        risk_records = [
            {
                "stock_code": row.get("stock_code") or row.get("code"),
                "stock_name": row.get("stock_name") or row.get("name"),
                "risk_score": row.get("risk_score"),
                "risk_level": row.get("risk_level"),
                "confidence": row.get("confidence"),
                "rank": row.get("pred_rank") or row.get("rank"),
                "trade_date": row.get("trade_date") or row.get("date"),
                "prediction_for_date": row.get("prediction_for_date"),
            }
            for row in rows
        ]
        return self._envelope(
            risk_records,
            message="Completed model risk inference queried.",
        )


model_inference_service = ModelInferenceService()
