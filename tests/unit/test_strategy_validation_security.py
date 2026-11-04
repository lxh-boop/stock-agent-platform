from __future__ import annotations

import json
from pathlib import Path

from agent_graph.services.strategy_implementation_service import (
    StrategyImplementationService,
)
from agent_graph.services.strategy_review_service import StrategyReviewService
from strategy_workflow_test_utils import database_path, save_draft


def test_strategy_validation_security_rejects_forbidden_import(tmp_path) -> None:
    draft = save_draft(
        tmp_path,
        proposal_json={
            "implementation_type": "code",
            "new_capability_spec": {"name": "dynamic_exposure"},
        },
    )
    proposal = draft.data["proposal"]
    implementation = StrategyImplementationService(
        db_path=database_path(tmp_path),
        runtime_dir=tmp_path / "runtime",
    ).lock_and_prepare(
        proposal_id=proposal["proposal_id"],
        proposal_version=1,
        user_id="u1",
        account_id="paper_u1",
        conversation_id="conv_1",
        run_id="run_security",
    )
    plugin = (
        Path(implementation.artifact_root)
        / "generated_code"
        / "strategy_plugin.py"
    )
    plugin.write_text("import subprocess\n", encoding="utf-8")

    reviewed = StrategyReviewService(
        db_path=database_path(tmp_path),
        runtime_dir=tmp_path / "runtime",
    ).validate_and_preview(
        implementation.implementation_id,
        user_id="u1",
    )
    report = json.loads(
        (Path(reviewed.artifact_root) / "security_report.json").read_text(
            encoding="utf-8"
        )
    )

    assert reviewed.status == "validation_failed"
    assert "forbidden_import:subprocess" in report["errors"]
