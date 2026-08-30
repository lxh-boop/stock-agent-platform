from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RankerMember:
    member_id: str
    weight: float
    checkpoint_path: Path
    model: lgb.Booster

    @property
    def features(self) -> list[str]:
        return list(self.model.feature_name())


class RegisteredRankEnsemble:
    """Score a cross-section with registered members and fuse percentile ranks."""

    def __init__(self, members: list[RankerMember]) -> None:
        if len(members) < 2:
            raise ValueError("秩融合至少需要两个模型成员")
        if any(member.weight <= 0.0 for member in members):
            raise ValueError("模型成员权重必须大于 0")
        self.members = list(members)

    @property
    def required_features(self) -> list[str]:
        return list(
            dict.fromkeys(
                feature
                for member in self.members
                for feature in member.features
            )
        )

    @classmethod
    def from_manifest(
        cls,
        manifest: dict[str, Any],
        *,
        model_dir: str | Path,
    ) -> "RegisteredRankEnsemble":
        root = Path(model_dir)
        members: list[RankerMember] = []
        for spec in list((manifest.get("fusion") or {}).get("members") or []):
            checkpoint = Path(str(spec.get("checkpoint") or ""))
            path = checkpoint if checkpoint.is_absolute() else root / checkpoint
            model = lgb.Booster(model_file=str(path))
            expected_count = int(spec.get("feature_count") or 0)
            if expected_count and len(model.feature_name()) != expected_count:
                raise RuntimeError(
                    f"模型成员特征数不一致：id={spec.get('id')}, "
                    f"expected={expected_count}, actual={len(model.feature_name())}"
                )
            members.append(
                RankerMember(
                    member_id=str(spec.get("id") or f"member_{len(members) + 1}"),
                    weight=float(spec.get("weight") or 0.0),
                    checkpoint_path=path,
                    model=model,
                )
            )
        return cls(members)

    def predict(self, panel: pd.DataFrame) -> pd.DataFrame:
        required_keys = {"date", "code"}
        if panel.empty or not required_keys.issubset(panel.columns):
            raise ValueError("截面推理面板为空或缺少 date/code")
        missing = [
            feature
            for feature in self.required_features
            if feature not in panel.columns
        ]
        if missing:
            raise RuntimeError(f"截面推理面板缺少模型特征：{missing[:12]}")

        out = panel.loc[:, ["date", "code"]].copy()
        weighted_rank = np.zeros(len(panel), dtype=np.float64)
        total_weight = sum(member.weight for member in self.members)
        for index, member in enumerate(self.members, start=1):
            raw_score = np.asarray(
                member.model.predict(panel.loc[:, member.features]),
                dtype=np.float64,
            )
            if raw_score.shape != (len(panel),) or not np.isfinite(raw_score).all():
                raise RuntimeError(f"模型成员输出无效：{member.member_id}")
            raw_column = f"member_{index}_raw_score"
            rank_column = f"member_{index}_rank_pct"
            out[raw_column] = raw_score.astype("float32")
            out[rank_column] = (
                pd.Series(raw_score, index=panel.index)
                .groupby(panel["date"], sort=False)
                .rank(pct=True, method="average")
                .to_numpy(dtype="float32")
            )
            weighted_rank += out[rank_column].to_numpy(dtype=np.float64) * member.weight
        out["model_score"] = (weighted_rank / total_weight).astype("float32")
        return out


__all__ = ["RankerMember", "RegisteredRankEnsemble"]
