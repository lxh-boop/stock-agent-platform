from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.services.model_inference_service import model_inference_service


class ModelMCPAdapter:
    """Adapter for completed active-ranker inference snapshots.

    Full-market inference remains an asynchronous Task Runtime operation and is
    deliberately absent from the synchronous MCP surface.
    """

    def __init__(self, *, db_path: str | Path | None) -> None:
        self.db_path = db_path

    def predict_stock_score(self, stock_code: str) -> dict[str, Any]:
        return model_inference_service.predict_stock_score(
            stock_code,
            db_path=self.db_path,
        )

    def predict_rank(self, top_k: int = 10) -> dict[str, Any]:
        return model_inference_service.predict_rank(
            top_k=top_k,
            db_path=self.db_path,
        )

    def predict_risk(self, stock_code: str) -> dict[str, Any]:
        return model_inference_service.predict_risk(
            stock_code,
            db_path=self.db_path,
        )
