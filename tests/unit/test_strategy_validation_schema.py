from __future__ import annotations

from agent_graph.services.strategy_review_service import StrategyReviewService


def test_strategy_validation_schema_checks_canonical_relations() -> None:
    valid = StrategyReviewService._validate_config(
        {
            "entry_top_k": 10,
            "hold_buffer_rank": 15,
            "max_positions": 10,
            "target_invested_weight": 0.80,
            "minimum_cash_ratio": 0.05,
            "min_rebalance_weight_delta": 0.01,
        }
    )
    invalid = StrategyReviewService._validate_config(
        {
            "entry_top_k": 8,
            "hold_buffer_rank": 6,
            "max_positions": 9,
            "target_invested_weight": 0.95,
            "minimum_cash_ratio": 0.10,
            "min_rebalance_weight_delta": 0.01,
        }
    )

    assert valid["status"] == "passed"
    assert invalid["status"] == "failed"
    assert "max_positions_must_not_exceed_entry_top_k" in invalid["errors"]
    assert "entry_top_k_must_not_exceed_hold_buffer_rank" in invalid["errors"]
    assert (
        "target_invested_weight_plus_minimum_cash_must_not_exceed_one"
        in invalid["errors"]
    )
