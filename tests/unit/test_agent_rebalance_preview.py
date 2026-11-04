from __future__ import annotations

from agent_graph.session.pending_action_store import get_pending_plan
from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper, preview_adjust_position_to_weight
from agent_control_center_utils import write_agent_fixture
from portfolio.paper_position import create_position
from portfolio.storage import PortfolioStorage


def test_agent_rebalance_preview_creates_confirmation_plan(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, price=10.0)
    result = preview_add_stock_to_paper("u1", "600519", output_dir=output_dir, db_path=db_path)
    assert result.success
    assert result.data["plan_id"].startswith("agent_plan_")
    assert result.data["confirmation_token"]
    assert get_pending_plan("u1", result.data["plan_id"], output_dir)


def test_agent_adjust_position_preview_creates_confirmation_plan(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, with_position=True, cash=100000.0)
    result = preview_adjust_position_to_weight(
        "u1",
        "000001",
        position_adjustment_ratio=0.5,
        output_dir=output_dir,
        db_path=db_path,
    )
    assert result.success
    assert result.data["action"] == "reduce"
    assert result.data["current_quantity"] == 1000.0
    assert result.data["target_quantity"] == 500.0
    assert result.data["estimated_quantity"] == 500.0
    assert get_pending_plan("u1", result.data["plan_id"], output_dir)


def test_agent_adjust_position_preview_rejects_sub_lot_half_reduce(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, with_position=False, cash=100000.0)
    storage = PortfolioStorage(db_path, output_dir=output_dir / "portfolio" / "u1")
    account = storage.load_account("paper_u1")
    assert account is not None
    storage.save_positions(
        [
            create_position(
                "u1",
                "000001",
                stock_name="Ping An Bank",
                quantity=100,
                cost_price=12.0,
                current_price=12.0,
                total_assets=account.total_assets,
            )
        ]
    )
    result = preview_adjust_position_to_weight(
        "u1",
        "000001",
        position_adjustment_ratio=0.5,
        output_dir=output_dir,
        db_path=db_path,
    )
    assert not result.success
    assert "no_executable_lot_quantity" in result.errors


def test_agent_adjust_position_preview_accepts_explicit_sell_lot(tmp_path) -> None:
    output_dir, db_path = write_agent_fixture(tmp_path, with_position=False, cash=100000.0)
    storage = PortfolioStorage(db_path, output_dir=output_dir / "portfolio" / "u1")
    account = storage.load_account("paper_u1")
    assert account is not None
    storage.save_positions(
        [
            create_position(
                "u1",
                "000001",
                stock_name="Ping An Bank",
                quantity=100,
                cost_price=12.0,
                current_price=12.0,
                total_assets=account.total_assets,
            )
        ]
    )
    result = preview_adjust_position_to_weight(
        "u1",
        "000001",
        requested_quantity=100,
        output_dir=output_dir,
        db_path=db_path,
    )
    assert result.success
    assert result.data["action"] == "reduce"
    assert result.data["current_quantity"] == 100.0
    assert result.data["target_quantity"] == 0.0
    assert result.data["estimated_quantity"] == 100.0
