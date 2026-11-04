from agent_graph.services.strategy_binding_service import StrategyBindingService
from agent_graph.services.strategy_config_compiler import StrategyConfigCompiler
from strategies.registry import StrategyManifest, get_strategy_registry
from strategy_position_test_utils import setup_position_account


def test_strategy_activation_preserves_current_positions(tmp_path) -> None:
    storage, before_account, before_positions = setup_position_account(tmp_path)
    config = StrategyConfigCompiler._canonical_config(
        {"entry_top_k": 7, "max_positions": 7}
    )
    manifest = StrategyManifest(
        strategy_id="phase7_activation",
        strategy_name="Phase 7 activation",
        version="v1",
        source_type="config_version",
        module_path="strategies.adapters.hierarchical_top10_strategy",
        class_name="HierarchicalTop10Strategy",
        status="registered_disabled",
        metadata={"config": config},
    )
    get_strategy_registry(
        output_dir=tmp_path / "outputs",
        db_path=tmp_path / "agent_quant.db",
    ).register(manifest)
    service = StrategyBindingService(
        db_path=tmp_path / "agent_quant.db",
        output_dir=tmp_path / "outputs",
    )
    plan = service.create_activation_plan(
        user_id="u1",
        account_id="paper_u1",
        strategy_id=manifest.strategy_id,
        strategy_version=manifest.version,
        effective_from="2026-07-16",
        conversation_id="conv_phase7",
        run_id="run_phase7",
    )
    result = service.commit(
        user_id="u1",
        plan_id=plan.data["plan_id"],
        confirmation_token=plan.confirmation_token,
        conversation_id="conv_phase7",
    )

    assert result.success
    assert storage.load_account("paper_u1").cash == before_account.cash
    assert storage.load_positions("u1") == before_positions
