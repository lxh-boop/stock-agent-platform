from __future__ import annotations

from pathlib import Path

from agent_graph.services.strategy_implementation_service import (
    canonical_json,
    sha256_file,
    sha256_text,
)
from strategy_workflow_test_utils import prepare_proposal


def test_implementation_artifact_hash_covers_manifest_files(tmp_path) -> None:
    _, result = prepare_proposal(
        tmp_path,
        {
            "implementation_type": "config",
            "config": {"target_ratio": 0.70, "min_cash_ratio": 0.15},
        },
    )
    root = Path(result.data["artifact_root"])
    manifest = result.data["artifact_manifest"]

    actual = {
        relative: sha256_file(root / relative)
        for relative in manifest["artifact_hashes"]
    }
    assert actual == manifest["artifact_hashes"]
    assert result.data["implementation_hash"] == sha256_text(
        canonical_json(actual)
    )
    assert len(result.data["artifact_manifest_hash"]) == 64
