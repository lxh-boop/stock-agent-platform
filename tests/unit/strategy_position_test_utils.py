from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_graph.services.strategy_position_service import StrategyPositionService
from database.connection import initialize_database
from portfolio.paper_account import create_default_account
from portfolio.paper_position import create_position
from portfolio.storage import PortfolioStorage


def position_recommendations():
    return [
        {
            "stock_code": f"{rank:06d}",
            "stock_name": f"S{rank}",
            "rank": rank,
            "original_rank": rank,
            "original_score": 1.0 - rank / 100.0,
            "current_price": 10.0,
            "industry": f"I{rank % 5}",
            "risk_level": "low",
            "trade_date": "2026-07-16",
        }
        for rank in range(1, 16)
    ]


def setup_position_account(tmp_path: Path, user_id: str = "u1"):
    db_path = tmp_path / "agent_quant.db"
    output_dir = tmp_path / "outputs"
    initialize_database(db_path)
    storage = PortfolioStorage(
        db_path,
        output_dir=output_dir / "portfolio" / user_id,
    )
    base = create_default_account(user_id, 100000.0)
    account = replace(
        base,
        cash=90000.0,
        total_assets=100000.0,
        position_market_value=10000.0,
    )
    positions = [
        create_position(
            user_id,
            "000015",
            "Old S15",
            quantity=1000.0,
            cost_price=10.0,
            current_price=10.0,
            total_assets=100000.0,
            industry="I0",
        )
    ]
    storage.save_account(account)
    storage.save_positions(positions)
    return storage, account, positions


def position_service(tmp_path: Path):
    return StrategyPositionService(
        db_path=tmp_path / "agent_quant.db",
        output_dir=tmp_path / "outputs",
    )


def create_position_preview(tmp_path: Path):
    return position_service(tmp_path).preview(
        user_id="u1",
        account_id="paper_u1",
        recommendations=position_recommendations(),
        trade_date="2026-07-16",
        conversation_id="conv_phase7",
        run_id="run_phase7",
    )
