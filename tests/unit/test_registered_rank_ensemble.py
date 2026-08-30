from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from ranking_runtime.settings import ACTIVE_MODEL_NAME, ACTIVE_MODEL_VERSION
from stock_ranker.registered_ensemble import RankerMember, RegisteredRankEnsemble


ROOT = Path(__file__).resolve().parents[2]


class _FakeBooster:
    def __init__(self, feature: str, multiplier: float = 1.0) -> None:
        self.feature = feature
        self.multiplier = multiplier

    def feature_name(self) -> list[str]:
        return [self.feature]

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return frame[self.feature].to_numpy(dtype=float) * self.multiplier


def test_registered_ensemble_fuses_daily_percentile_ranks() -> None:
    model = RegisteredRankEnsemble(
        [
            RankerMember("member_1", 0.5, Path("one"), _FakeBooster("left")),
            RankerMember("member_2", 0.5, Path("two"), _FakeBooster("right")),
        ]
    )
    panel = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-08-10"] * 3),
            "code": ["000001", "000002", "000003"],
            "left": [3.0, 2.0, 1.0],
            "right": [1.0, 3.0, 2.0],
        }
    )

    scored = model.predict(panel)

    assert model.required_features == ["left", "right"]
    assert np.allclose(scored["member_1_rank_pct"], [1.0, 2 / 3, 1 / 3])
    assert np.allclose(scored["member_2_rank_pct"], [1 / 3, 1.0, 2 / 3])
    assert np.allclose(scored["model_score"], [2 / 3, 5 / 6, 0.5])


def test_active_manifest_uses_stable_public_identity_and_generic_member_files() -> None:
    manifest = json.loads(
        (ROOT / "configs" / "active_ranker.json").read_text(encoding="utf-8")
    )

    assert manifest["alias"] == ACTIVE_MODEL_NAME
    assert manifest["version"] == ACTIVE_MODEL_VERSION
    assert [member["checkpoint"] for member in manifest["fusion"]["members"]] == [
        "member_1.txt",
        "member_2.txt",
    ]
    assert manifest["fusion"]["method"] == "daily_percentile_rank_weighted_mean"
    assert manifest["selection_count"] == 15
    assert manifest["validation"]["target_met"] is False
