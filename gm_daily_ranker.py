from __future__ import annotations

"""Single-model contract for the GM daily rolling update."""

from hashlib import sha256
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from data_gm import GM_DAILY_FEATURE_COLUMNS


MODEL_NAME = "gm_daily_ranker"


class GmDailyRanker:
    """A LightGBM booster with one fixed GM daily-bar feature contract."""

    def __init__(self, model: Any, model_path: str | Path):
        feature_names = list(getattr(model, "feature_name", lambda: [])())
        if feature_names and feature_names != GM_DAILY_FEATURE_COLUMNS:
            raise ValueError(
                "gm_model_feature_contract_mismatch:"
                f"expected={GM_DAILY_FEATURE_COLUMNS},actual={feature_names}"
            )
        self.model = model
        self.model_path = Path(model_path)
        self.model_version = self._version(self.model_path)

    @staticmethod
    def _version(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()[:12]

    @classmethod
    def from_file(cls, path: str | Path) -> "GmDailyRanker":
        model_path = Path(path).expanduser()
        if not model_path.is_file():
            raise FileNotFoundError(f"gm_model_file_not_found:{model_path}")
        try:
            import lightgbm as lgb
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("lightgbm_not_installed") from exc
        return cls(lgb.Booster(model_file=str(model_path)), model_path)

    def predict(self, panel: pd.DataFrame) -> pd.DataFrame:
        missing = [column for column in GM_DAILY_FEATURE_COLUMNS if column not in panel.columns]
        if missing:
            raise ValueError(f"gm_feature_panel_missing_columns:{missing}")
        if panel.empty:
            raise ValueError("gm_feature_panel_empty")

        features = panel.loc[:, GM_DAILY_FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce")
        features = features.replace([np.inf, -np.inf], np.nan)
        raw_score = np.asarray(self.model.predict(features), dtype=np.float64)
        if raw_score.shape != (len(panel),) or not np.isfinite(raw_score).all():
            raise RuntimeError("gm_model_output_invalid")

        out = panel.loc[:, ["date", "code", "symbol", "name"]].copy()
        out["raw_score"] = raw_score
        out["pred_score"] = (
            pd.Series(raw_score, index=panel.index)
            .groupby(panel["date"], sort=False)
            .rank(pct=True, method="average")
            .to_numpy(dtype=np.float64)
        )
        out = out.sort_values(
            ["pred_score", "raw_score", "code"], ascending=[False, False, True], kind="stable"
        ).reset_index(drop=True)
        out["pred_rank"] = np.arange(1, len(out) + 1)
        return out


__all__ = ["GM_DAILY_FEATURE_COLUMNS", "MODEL_NAME", "GmDailyRanker"]
