from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from config import (
    ACTIVE_RANKING_MODEL_BACKEND,
    ACTIVE_RANKING_MODEL_DIR,
    ACTIVE_RANKING_MODEL_MANIFEST_PATH,
    ACTIVE_RANKING_MODEL_NAME,
    ACTIVE_RANKING_MODEL_VERSION,
)


ACTIVE_MODEL_NAME = ACTIVE_RANKING_MODEL_NAME
ACTIVE_MODEL_BACKEND = ACTIVE_RANKING_MODEL_BACKEND
ACTIVE_MODEL_VERSION = ACTIVE_RANKING_MODEL_VERSION
ACTIVE_MODEL_DIR = Path(ACTIVE_RANKING_MODEL_DIR)
ACTIVE_MODEL_MANIFEST_PATH = Path(ACTIVE_RANKING_MODEL_MANIFEST_PATH)


def load_active_model_manifest(
    path: str | Path | None = None,
) -> dict[str, Any]:
    manifest_path = Path(path) if path is not None else ACTIVE_MODEL_MANIFEST_PATH
    if not manifest_path.is_file():
        raise FileNotFoundError(f"主动排名模型注册清单不存在：{manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("主动排名模型注册清单必须是 JSON object")
    if str(payload.get("alias") or "") != ACTIVE_MODEL_NAME:
        raise ValueError(
            "主动排名模型别名与运行配置不一致："
            f"manifest={payload.get('alias')}, runtime={ACTIVE_MODEL_NAME}"
        )
    if str(payload.get("version") or "") != ACTIVE_MODEL_VERSION:
        raise ValueError(
            "主动排名模型版本与运行配置不一致："
            f"manifest={payload.get('version')}, runtime={ACTIVE_MODEL_VERSION}"
        )
    members = list((payload.get("fusion") or {}).get("members") or [])
    if len(members) < 2:
        raise ValueError("主动排名模型至少需要两个秩融合成员")
    return payload


def resolve_checkpoint_path(
    member: dict[str, Any],
    *,
    model_dir: str | Path | None = None,
) -> Path:
    root = Path(model_dir) if model_dir is not None else ACTIVE_MODEL_DIR
    checkpoint = Path(str(member.get("checkpoint") or ""))
    if not checkpoint.name:
        raise ValueError("模型成员缺少 checkpoint")
    return checkpoint if checkpoint.is_absolute() else root / checkpoint


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_active_model_assets(
    *,
    manifest_path: str | Path | None = None,
    model_dir: str | Path | None = None,
    verify_hashes: bool = True,
) -> dict[str, Any]:
    try:
        manifest = load_active_model_manifest(manifest_path)
    except Exception as exc:
        return {
            "ready": False,
            "model_name": ACTIVE_MODEL_NAME,
            "model_backend": ACTIVE_MODEL_BACKEND,
            "model_version": ACTIVE_MODEL_VERSION,
            "error": f"{type(exc).__name__}: {exc}",
        }

    member_reports: list[dict[str, Any]] = []
    ready = True
    for member in list((manifest.get("fusion") or {}).get("members") or []):
        path = resolve_checkpoint_path(member, model_dir=model_dir)
        exists = path.is_file()
        expected = str(member.get("sha256") or "").lower()
        actual = _sha256(path) if exists and verify_hashes and expected else ""
        hash_matches = bool(exists and (not expected or not verify_hashes or actual == expected))
        ready = ready and exists and hash_matches
        member_reports.append(
            {
                "id": str(member.get("id") or ""),
                "path": str(path),
                "exists": exists,
                "size_bytes": path.stat().st_size if exists else 0,
                "feature_count": int(member.get("feature_count") or 0),
                "weight": float(member.get("weight") or 0.0),
                "sha256_matches": hash_matches,
            }
        )
    return {
        "ready": ready,
        "model_name": ACTIVE_MODEL_NAME,
        "display_name": str(manifest.get("display_name") or ACTIVE_MODEL_NAME),
        "model_backend": ACTIVE_MODEL_BACKEND,
        "model_version": ACTIVE_MODEL_VERSION,
        "engine": str(manifest.get("engine") or ""),
        "fusion_method": str((manifest.get("fusion") or {}).get("method") or ""),
        "selection_count": int(manifest.get("selection_count") or 15),
        "members": member_reports,
    }


__all__ = [
    "ACTIVE_MODEL_BACKEND",
    "ACTIVE_MODEL_DIR",
    "ACTIVE_MODEL_MANIFEST_PATH",
    "ACTIVE_MODEL_NAME",
    "ACTIVE_MODEL_VERSION",
    "load_active_model_manifest",
    "resolve_checkpoint_path",
    "validate_active_model_assets",
]
