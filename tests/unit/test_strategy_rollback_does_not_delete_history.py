from agent_graph.services.strategy_binding_service import StrategyBindingService
from strategies.binding_repository import StrategyBindingRepository
from strategy_position_test_utils import setup_position_account
from strategy_runtime_test_utils import bind_runtime


def test_strategy_rollback_does_not_delete_history(tmp_path) -> None:
    storage, _, _ = setup_position_account(tmp_path)
    manifest, first = bind_runtime(tmp_path)
    repository = StrategyBindingRepository(
        tmp_path / "agent_quant.db"
    )
    second = repository.activate(
        user_id="u1",
        account_id="paper_u1",
        strategy_id=manifest.strategy_id,
        strategy_version=manifest.version,
        config_hash=first.config_hash,
        effective_from="2026-01-02",
        source_plan_id="plan_phase7_second",
    )
    storage.save_strategy_execution_history(
        {
            "user_id": "u1",
            "account_id": "paper_u1",
            "trade_date": "2026-07-15",
            "run_id": "historical_run",
            "strategy_id": manifest.strategy_id,
            "strategy_version": manifest.version,
            "binding_id": second.binding_id,
            "config_hash": first.config_hash,
            "resolved_config": {},
            "positions_before": [],
            "target_portfolio": [],
            "orders": [],
            "positions_after": [],
            "cash_before": 100000.0,
            "cash_after": 100000.0,
        }
    )
    service = StrategyBindingService(
        db_path=tmp_path / "agent_quant.db",
        output_dir=tmp_path / "outputs",
    )
    plan = service.create_rollback_plan(
        user_id="u1",
        account_id="paper_u1",
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
    assert len(
        repository.list_history(
            user_id="u1",
            account_id="paper_u1",
        )
    ) == 3
    assert len(storage.list_strategy_execution_history("u1")) == 1
