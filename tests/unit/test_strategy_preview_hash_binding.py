from __future__ import annotations

from pathlib import Path

from agent_graph.services.strategy_implementation_service import sha256_file
from strategy_workflow_test_utils import prepare_proposal


def test_strategy_preview_hash_is_bound_in_artifact_manifest(tmp_path) -> None:
    _, result = prepare_proposal(
        tmp_path,
        {
            "implementation_type": "config",
            "config": {"entry_top_k": 9, "max_positions": 9},
        },
    )
    root = Path(result.data["artifact_root"])
    manifest = result.data["artifact_manifest"]

    assert manifest["implementation_preview_hash"] == sha256_file(
        root / "implementation_preview.json"
    )
    assert (
        manifest["artifact_hashes"]["implementation_preview.json"]
        == manifest["implementation_preview_hash"]
    )
    assert manifest["validation_status"] == "passed"
