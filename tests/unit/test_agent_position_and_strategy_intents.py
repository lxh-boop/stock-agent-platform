from __future__ import annotations

from agent_graph.session.pending_action_store import get_pending_plan
from agent_graph.domain_tools.manual_position_operation_tool import preview_manual_position_operation
from agent_graph.domain_tools.strategy_builder_tool import prepare_strategy_change
from agent_graph.domain_tools.strategy_management_tool import execute_confirmed_strategy_plan
from agent_control_center_utils import write_agent_fixture
from strategies.registry import get_strategy_registry
from strategies.security import scan_strategy_source


def test_manual_position_operation_creates_one_time_plan(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, with_position=True, cash=100000.0)

    result = preview_manual_position_operation(
        "u1",
        stock_code="000001",
        position_adjustment_ratio=0.5,
        query="今天把 000001 减半",
        output_dir=output_dir,
        db_path=db_path,
    )

    assert result.success
    assert result.requires_confirmation
    assert result.data["operation_type"] == "one_time_position_operation"
    assert result.data["source_type"] == "manual_one_time_operation"
    assert result.data["long_term_strategy_changed"] is False

    plan = get_pending_plan("u1", result.data["plan_id"], output_dir)
    assert plan is not None
    assert plan["operation_type"] == "one_time_position_operation"
    assert plan["expires_after_execution"] is True
    assert plan["validation_results"]["long_term_strategy_changed"] is False


def test_strategy_change_registers_then_requires_enable_confirmation(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path)
    preview = prepare_strategy_change(
        "u1",
        "以后只持有模型排名前 5 的股票",
        parameters={"top_k": 5},
        output_dir=output_dir,
        db_path=db_path,
    )

    assert preview.success
    assert preview.requires_confirmation
    assert preview.data["operation_type"] == "strategy_change"
    assert preview.data["implementation_type"] == "existing_strategy_config"

    registered = execute_confirmed_strategy_plan(
        "u1",
        preview.data["plan_id"],
        preview.data["confirmation_token"],
        output_dir=output_dir,
        db_path=db_path,
    )
    assert registered.success
    enable_plan = registered.data["enable_plan"]
    assert enable_plan["strategy_id"] == preview.data["strategy_id"]

    enabled = execute_confirmed_strategy_plan(
        "u1",
        enable_plan["plan_id"],
        enable_plan["confirmation_token"],
        output_dir=output_dir,
        db_path=db_path,
    )
    assert enabled.success

    registry = get_strategy_registry(output_dir=output_dir, db_path=db_path)
    manifest = registry.get(preview.data["strategy_id"], preview.data["strategy_version"])
    assert manifest is not None
    assert manifest.enabled_for_paper_trading is True


def test_vague_strategy_style_does_not_create_style_layer(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path)
    result = prepare_strategy_change(
        "u1",
        "以后更稳健一点",
        output_dir=output_dir,
        db_path=db_path,
    )

    assert not result.success
    assert "insufficient_strategy_rule" in result.errors
    assert not result.requires_confirmation
    assert "registered_strategies" in result.data


def test_strategy_security_scan_rejects_banned_import() -> None:
    source = "import os\nfrom strategies.base import StrategyResult\n"
    scan = scan_strategy_source(source)
    assert not scan.passed
    assert "banned_import:os" in scan.errors
