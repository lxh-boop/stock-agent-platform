from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from application.web_paper_trading_service import WebPaperTradingApplicationService
from application.web_read_service import web_read_service
from portfolio.paper_account import create_default_account
from portfolio.paper_order import create_paper_order
from portfolio.paper_position import create_position
from portfolio.storage import PortfolioStorage
from server.api.main import create_app
from server.api.presenters.paper_trading import present_daily_history, present_write_result
from server.api.schemas.paper_trading import ProfileUpdateRequest, ProposalCommitRequest

ROOT = Path(__file__).resolve().parents[2]


def test_stage6_3_paper_routes_and_methods() -> None:
    paths = create_app().openapi()["paths"]
    prefix = "/api/v1/web/paper-trading"
    expected = {
        f"{prefix}/summary": {"get"},
        f"{prefix}/account": {"get"},
        f"{prefix}/positions": {"get"},
        f"{prefix}/orders": {"get"},
        f"{prefix}/history": {"get"},
        f"{prefix}/profile": {"get", "put"},
        f"{prefix}/cash-flows": {"get"},
        f"{prefix}/cash-flows/preview": {"post"},
        f"{prefix}/cash-flows/{{cash_flow_id}}/cancel": {"post"},
        f"{prefix}/backfill/preview": {"post"},
        f"{prefix}/proposals": {"get"},
        f"{prefix}/proposals/{{plan_id}}": {"get"},
        f"{prefix}/proposals/{{plan_id}}/commit": {"post"},
        f"{prefix}/proposals/{{plan_id}}/reject": {"post"},
    }
    assert expected.keys() <= paths.keys()
    for path, methods in expected.items():
        assert methods <= set(paths[path])


def test_write_request_requires_idempotency_metadata() -> None:
    with pytest.raises(ValidationError):
        ProfileUpdateRequest(user_id="u", profile={}, confirmed=True)
    with pytest.raises(ValidationError):
        ProposalCommitRequest(user_id="u", confirmation_text="CONFIRM-123456")


def test_proposal_summary_never_exposes_confirmation_secret() -> None:
    service = WebPaperTradingApplicationService(output_dir="outputs", db_path=None)
    summary = service._proposal_summary(
        {
            "plan_id": "plan-abcdef123456",
            "intent": "capital_change",
            "confirmation_token": "super-secret-token",
            "plan_hash": "private-plan-hash",
            "proposed_changes": [{"amount": 1000}],
        }
    )
    encoded = json.dumps(present_write_result(summary), ensure_ascii=False)
    nested = service._strip_write_secrets({
        "data": {
            "confirmation_token": "super-secret-token",
            "plan_hash": "private-plan-hash",
            "plan_id": "plan-abcdef123456",
        }
    })
    nested_encoded = json.dumps(present_write_result(nested), ensure_ascii=False)
    assert "super-secret-token" not in encoded + nested_encoded
    assert "private-plan-hash" not in encoded + nested_encoded
    assert summary["confirmation_phrase"] == "CONFIRM-123456"


def test_backfill_commit_is_rejected_outside_task_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("agent.session.pending_action_store")
    module.get_pending_plan = lambda *args, **kwargs: {
        "plan_id": "plan-backfill-123456",
        "intent": "paper_backfill",
        "confirmation_token": "server-only",
    }
    monkeypatch.setitem(sys.modules, "agent.session.pending_action_store", module)
    service = WebPaperTradingApplicationService(output_dir="outputs", db_path=None)
    with pytest.raises(ValueError, match="paper_backfill_requires_task_runtime"):
        service.commit_proposal(
            user_id="u",
            plan_id="plan-backfill-123456",
            confirmation_text="CONFIRM-123456",
            request_id="request-1",
            idempotency_key="idem-1",
        )


def test_new_backfill_task_is_additive_and_registered() -> None:
    contract = json.loads((ROOT / "contracts/stage6/task-contract.json").read_text(encoding="utf-8"))
    manager_source = (ROOT / "server/task_runtime/manager.py").read_text(encoding="utf-8")
    handler_source = (ROOT / "server/task_runtime/handlers.py").read_text(encoding="utf-8")
    assert "paper-trading.backfill" in contract["task_types"]
    assert '"paper-trading.backfill"' in manager_source
    assert 'task_type == "paper-trading.backfill"' in handler_source


def test_task_store_persists_only_recovery_metadata() -> None:
    source = (ROOT / "frontend/src/stores/taskStore.ts").read_text(encoding="utf-8")
    partial = source.split("partialize:", 1)[1]
    assert "activeTaskId" in partial and "lastSequence" in partial
    assert "events: state.events" not in partial
    assert "task: state.task" not in partial


def test_react_write_controls_require_confirmation_and_use_named_actions() -> None:
    task_actions = (ROOT / "frontend/src/components/paper/PaperTaskActions.tsx").read_text(encoding="utf-8")
    profile_form = (ROOT / "frontend/src/components/paper/UserProfileForm.tsx").read_text(encoding="utf-8")
    proposal_panel = (ROOT / "frontend/src/components/paper/ProposalPanel.tsx").read_text(encoding="utf-8")
    for label in ("更新 AI 模拟盘", "运行新闻调整", "手动运行调度器"):
        assert label in task_actions
    assert "Modal.confirm" in task_actions
    assert "Modal.confirm" in profile_form
    assert "confirmation_phrase" in proposal_panel
    assert "paper-trading.backfill" in proposal_panel


def test_paper_sections_are_collapsible_and_layout_scrolls_independently() -> None:
    section_source = (ROOT / "frontend/src/components/paper/PaperSectionCard.tsx").read_text(encoding="utf-8")
    page_source = (ROOT / "frontend/src/pages/paper/PaperTradingPage.tsx").read_text(encoding="utf-8")
    layout_source = (ROOT / "frontend/src/layouts/AppLayout.tsx").read_text(encoding="utf-8")
    css_source = (ROOT / "frontend/src/styles/global.css").read_text(encoding="utf-8")
    for section_key in (
        "account-summary", "task-actions", "user-profile", "asset-curve", "daily-history",
        "paper-records", "risk-diagnostics", "cash-flow", "backfill", "proposals",
    ):
        assert section_key in section_source
    assert "全部展开" in section_source and "全部收起" in section_source
    assert "PaperSectionProvider" in page_source and "PaperSectionControls" in page_source
    assert 'className="app-sider-menu-scroll"' in layout_source
    assert 'className="app-content-scroll"' in layout_source
    assert "overflow-y: auto" in css_source
    assert "html, body, #root { height: 100%; margin: 0; overflow: hidden; }" in css_source


def test_daily_history_returns_exact_positions_trades_and_same_day_ohlc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs"
    storage = PortfolioStorage(
        tmp_path / "agent.db",
        output_dir=output_dir / "portfolio" / "u1",
        use_database=False,
    )
    position = create_position(
        "u1",
        "000001",
        stock_name="平安银行",
        quantity=100,
        cost_price=10,
        current_price=11,
        total_assets=100000,
    )
    account = replace(
        create_default_account("u1", 100000),
        cash=98900,
        position_market_value=1100,
        total_assets=100000,
    )
    order = create_paper_order(
        user_id="u1",
        trade_date="2026-07-28",
        stock_code="000001",
        stock_name="平安银行",
        action="buy",
        target_weight=0.011,
        executed_price=10.8,
        quantity=100,
        reason="daily verification",
    )
    storage.write_daily_snapshot(
        account=account,
        positions=[position],
        orders=[order],
        trade_date="2026-07-28",
    )
    monkeypatch.setattr(
        web_read_service,
        "load_signal_ohlc_data",
        lambda: pd.DataFrame(
            [
                {
                    "date": "2026-07-28",
                    "code": "000001",
                    "open": 10.5,
                    "high": 11.2,
                    "low": 10.3,
                    "close": 11.0,
                }
            ]
        ),
    )

    service = WebPaperTradingApplicationService(output_dir=output_dir, db_path=None)
    payload = present_daily_history(service.daily_history("u1", "2026-07-28"))

    assert payload["available_dates"] == ["2026-07-28"]
    assert payload["has_position_snapshot"] is True
    assert payload["positions"]["total"] == 1
    assert payload["operations"]["total"] == 1
    assert payload["buy_operations"]["total"] == 1
    assert payload["sell_operations"]["total"] == 0
    assert payload["summary"] == {
        "position_count": 1,
        "operation_count": 1,
        "buy_count": 1,
        "sell_count": 0,
        "ohlc_matched_count": 1,
        "ohlc_missing_count": 0,
        "close_matched_count": 1,
    }
    operation = payload["operations"]["records"][0]
    assert operation["stock_code"] == "000001"
    assert operation["action"] == "buy"
    assert operation["trade_record_id"] == "2026-07-28_000001"
    assert [operation[key] for key in ("open", "high", "low", "close")] == [10.5, 11.2, 10.3, 11.0]
    assert operation["ohlc_available"] is True

    missing_day = present_daily_history(service.daily_history("u1", "2026-07-29"))
    assert missing_day["has_position_snapshot"] is False
    assert missing_day["positions"]["total"] == 0
    assert missing_day["operations"]["total"] == 0
    assert missing_day["buy_operations"]["total"] == 0
    assert missing_day["sell_operations"]["total"] == 0


def test_sell_history_traces_each_prior_buy_lot_by_date_and_stock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "outputs"
    storage = PortfolioStorage(
        tmp_path / "agent.db",
        output_dir=output_dir / "portfolio" / "u1",
        use_database=False,
    )
    orders = [
        create_paper_order(
            user_id="u1",
            trade_date="2026-07-25",
            stock_code="000001",
            action="buy",
            target_weight=0.01,
            executed_price=10.2,
            quantity=100,
            reason="first lot",
        ),
        create_paper_order(
            user_id="u1",
            trade_date="2026-07-26",
            stock_code="000001",
            action="sell",
            target_weight=0.0,
            executed_price=10.4,
            quantity=100,
            reason="prior exit consumes first lot",
        ),
        create_paper_order(
            user_id="u1",
            trade_date="2026-07-28",
            stock_code="000001",
            action="buy",
            target_weight=0.02,
            executed_price=10.8,
            quantity=200,
            reason="second lot",
        ),
        create_paper_order(
            user_id="u1",
            trade_date="2026-07-29",
            stock_code="000001",
            action="buy",
            target_weight=0.01,
            executed_price=11.0,
            quantity=100,
            reason="third lot",
        ),
        create_paper_order(
            user_id="u1",
            trade_date="2026-07-30",
            stock_code="000001",
            action="sell",
            target_weight=0.0,
            executed_price=11.5,
            quantity=250,
            reason="exit",
        ),
    ]
    position_quantities = {
        "2026-07-25": 100,
        "2026-07-26": 0,
        "2026-07-28": 200,
        "2026-07-29": 300,
        "2026-07-30": 50,
    }
    for order in orders:
        position_quantity = position_quantities[order.trade_date]
        storage.write_daily_snapshot(
            orders=[order],
            positions=(
                [
                    create_position(
                        "u1",
                        "000001",
                        quantity=position_quantity,
                        cost_price=order.executed_price,
                        current_price=order.executed_price,
                        total_assets=100000,
                    )
                ]
                if position_quantity > 0
                else []
            ),
            trade_date=order.trade_date,
        )

    monkeypatch.setattr(
        web_read_service,
        "load_signal_ohlc_data",
        lambda: pd.DataFrame(
            [{
                "date": "2026-07-30",
                "code": "000001",
                "open": 11.2,
                "high": 11.8,
                "low": 11.0,
                "close": 11.5,
            }]
        ),
    )
    service = WebPaperTradingApplicationService(output_dir=output_dir, db_path=None)
    payload = present_daily_history(service.daily_history("u1", "2026-07-30"))

    assert payload["buy_operations"]["total"] == 0
    assert payload["sell_operations"]["total"] == 1
    sell = payload["sell_operations"]["records"][0]
    assert sell["trade_record_id"] == "2026-07-30_000001"
    assert sell["purchase_lot_count"] == 2
    assert [lot["lot_id"] for lot in sell["purchase_lots"]] == [
        "2026-07-28_000001",
        "2026-07-29_000001",
    ]
    assert [lot["executed_price"] for lot in sell["purchase_lots"]] == [10.8, 11.0]
    assert [lot["quantity"] for lot in sell["purchase_lots"]] == [200.0, 50.0]
    assert [lot["original_quantity"] for lot in sell["purchase_lots"]] == [200.0, 100.0]
    assert sell["matched_quantity"] == 250.0
    assert sell["unmatched_quantity"] == 0.0
    assert sell["matching_method"] == "position_snapshot_recent_lots"
    assert sell["position_snapshot_date"] == "2026-07-29"
    assert sell["position_quantity_before"] == 300.0
    assert sell["cycle_start_date"] == "2026-07-26"
    assert all(float(lot["total_fee"]) > 0 for lot in sell["purchase_lots"])


def test_daily_history_frontend_uses_one_date_and_shows_ohlc() -> None:
    component = (ROOT / "frontend/src/components/paper/DailyHistoryPanel.tsx").read_text(encoding="utf-8")
    api = (ROOT / "frontend/src/api/paperTradingApi.ts").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/pages/paper/PaperTradingPage.tsx").read_text(encoding="utf-8")

    assert 'type="date"' in component
    assert "当日买入操作" in component
    assert "当日卖出操作" in component
    assert "当日收盘后历史持仓" in component
    assert "买入费用" in component
    assert "purchase_lots" in component
    assert "trade_record_id" in component
    assert "以卖出前持仓快照为准" in component
    assert "本次匹配数量" in component
    for label in ("开盘", "最高", "最低", "收盘"):
        assert label in component
    assert "paperTradingApi.history" in component
    assert "trade_date: tradeDate" in api
    assert "<DailyHistoryPanel" in page
