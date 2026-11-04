from __future__ import annotations

from types import SimpleNamespace

from agent_graph.financial_graph.providers.portfolio import PortfolioGraphProvider
from agent_graph.services.portfolio_service import PortfolioService
from portfolio.paper_account import account_from_dict
from portfolio.storage import PortfolioStorage


def test_portfolio_service_exposes_success_and_found_contract(tmp_path) -> None:
    storage = PortfolioStorage(output_dir=tmp_path / "portfolio" / "u1", use_database=False)
    storage.save_account(account_from_dict({"account_id": "paper_u1", "user_id": "u1", "cash": 100000}))

    state = PortfolioService().get_portfolio_state("u1", output_dir=tmp_path)

    assert state["success"] is True
    assert state["found"] is True
    assert state["account"]["account_id"] == "paper_u1"


def test_portfolio_provider_does_not_treat_missing_success_key_as_failure(tmp_path, monkeypatch) -> None:
    graph = SimpleNamespace(
        upsert_snapshot=lambda **_: (
            SimpleNamespace(to_dict=lambda: {"node_id": "portfolio:u1"}),
            {"holding_refs": [], "unresolved_positions": [], "applied": {}},
        )
    )
    provider = PortfolioGraphProvider(portfolio_graph=graph)
    monkeypatch.setattr(
        "agent_graph.services.portfolio_service.PortfolioService.get_portfolio_state",
        lambda self, **kwargs: {
            "found": True,
            "account": {"account_id": "paper_u1"},
            "positions": [],
            "warnings": [],
            "errors": [],
        },
    )

    result = provider.load_portfolio_snapshot(
        user_id="u1",
        output_dir=tmp_path,
        db_path=None,
        as_of_time="",
        source_task_id="task-1",
        source_agent_id="PORTFOLIO_ANALYST",
    )

    assert result["success"] is True


def test_missing_account_is_classified_as_business_result_empty(tmp_path) -> None:
    provider = PortfolioGraphProvider(portfolio_graph=SimpleNamespace())

    result = provider.load_portfolio_snapshot(
        user_id="u1",
        output_dir=tmp_path,
        db_path=None,
        as_of_time="",
        source_task_id="task-1",
        source_agent_id="PORTFOLIO_ANALYST",
    )

    assert result["success"] is False
    assert result["error_type"] == "portfolio_account_not_initialized"
    assert result["failure_kind"] == "business_result_empty"
    assert result["retryable"] is False
