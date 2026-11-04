from __future__ import annotations

import json

from agent_graph.services.strategy_audit_service import StrategyAuditService
from strategy_binding_test_utils import (
    confirm_binding,
    create_binding_plan,
    register_strategy,
)
from strategy_position_test_utils import (
    create_position_preview,
    position_service,
    setup_position_account,
)


def test_strategy_audit_trace_reconstructs_apply_activation_and_execution(
    tmp_path,
):
    setup_position_account(tmp_path)
    manifest = register_strategy(tmp_path)
    activation_plan = create_binding_plan(
        tmp_path,
        manifest,
        effective_from="2026-07-16",
    )
    activated = confirm_binding(tmp_path, activation_plan)
    assert activated.success
    binding = activated.data["binding"]

    position_plan = create_position_preview(tmp_path)
    assert position_plan.success, position_plan.to_dict()
    executed = position_service(tmp_path).commit(
        user_id="u1",
        plan_id=position_plan.data["plan_id"],
        confirmation_token=position_plan.confirmation_token,
        conversation_id="conv_phase7",
    )
    assert executed.success

    trace = StrategyAuditService(
        db_path=tmp_path / "agent_quant.db",
        output_dir=tmp_path / "outputs",
    ).trace(
        user_id="u1",
        binding_id=binding["binding_id"],
    )

    assert trace["proposal_versions"]
    assert trace["locked_version"] == 1
    assert trace["implementation"]["implementation_id"]
    assert trace["artifact_manifest"]["implementation_id"]
    assert trace["application_plan"]["intent"] == (
        "apply_strategy_implementation"
    )
    assert trace["application_commit"]
    assert trace["registration_result"]["status"] == (
        "registered_disabled"
    )
    assert trace["activation_plan"]["intent"] == (
        "activate_strategy_binding"
    )
    assert trace["activation_commit"]
    assert trace["binding"]["binding_id"] == binding["binding_id"]
    assert trace["actual_strategy_executions"]
    execution = trace["actual_strategy_executions"][-1]
    assert execution["positions_before"]
    assert execution["positions_after"]
    assert execution["strategy_version"] == manifest["version"]

    rendered = json.dumps(trace, ensure_ascii=False)
    assert "confirmation_token" not in rendered
    assert "confirmation_token_hash" not in rendered

    identifiers = [
        {"proposal_id": manifest["metadata"]["proposal_id"]},
        {
            "implementation_id": manifest["metadata"][
                "implementation_id"
            ]
        },
        {"plan_id": manifest["metadata"]["source_plan_id"]},
        {
            "commit_id": (
                "commit_"
                + manifest["metadata"]["source_plan_id"]
            )
        },
        {"binding_id": binding["binding_id"]},
        {"run_id": execution["run_id"]},
        {"conversation_id": "conv_1"},
    ]
    service = StrategyAuditService(
        db_path=tmp_path / "agent_quant.db",
        output_dir=tmp_path / "outputs",
    )
    for identifier in identifiers:
        linked = service.trace(user_id="u1", **identifier)
        assert linked["proposal"]
        assert linked["implementation"]
        assert linked["registration_result"]
        assert linked["binding"]
