from __future__ import annotations

from pipelines.paper_trading_pipeline import run_paper_trading_pipeline
from pipelines.schemas import PipelineContext, PipelineStatus
from scoring.schemas import FusionOutput


def _output() -> FusionOutput:
    return FusionOutput(
        user_id="u1",
        trade_date="2026-06-11",
        stock_code="000001",
        original_pred_score=0.9,
        original_pred_rank=1,
        original_score=0.9,
        original_rank=1,
        news_adjustment=0,
        user_adjustment=0,
        combined_adjustment=0,
        position_adjustment_ratio=1,
        target_weight=0.08,
        confidence="high",
    )


def test_paper_trading_pipeline_generates_only_paper_trading(tmp_path) -> None:
    context = PipelineContext(user_id="u1", trade_date="2026-06-11", output_dir=tmp_path / "outputs", db_path=tmp_path / "agent_quant.db", paper_trading_enabled=True)

    result = run_paper_trading_pipeline(context, [_output()])

    assert result.status == PipelineStatus.SUCCESS
    assert result.is_paper_trading is True
    assert all(order.is_paper_trading for order in result.orders)
    assert not any(getattr(order, "broker", None) for order in result.orders)


def test_paper_trading_pipeline_dry_run_generates_plan_only(tmp_path) -> None:
    context = PipelineContext(user_id="u1", trade_date="2026-06-11", output_dir=tmp_path / "outputs", db_path=tmp_path / "agent_quant.db", dry_run=True)

    result = run_paper_trading_pipeline(context, [_output()])

    assert result.status == PipelineStatus.SUCCESS
    assert result.orders == []
    assert result.plan is not None
    assert result.graph_sync == {
        "success": True,
        "status": "skipped_plan_only",
    }
    assert not any("graph_sync_failed" in warning for warning in result.warnings)
    assert not (tmp_path / "outputs" / "portfolio" / "paper_orders.csv").exists()
