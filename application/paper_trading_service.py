from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from agent_graph.domain_tools.dispatch import AGENT_MAIN, execute_tool as _execute_tool
from agent_graph.writes import execute_confirmed_plan_v2 as _execute_confirmed_plan_v2
from config import DEFAULT_INITIAL_CASH, DEFAULT_PAPER_TRADING_START_DATE
from evaluation.evaluation_store import load_ai_reliability_state
from news_db_sync import sync_event_cache_to_agent_db
from pipelines.paper_backfill_pipeline import load_paper_backfill_status
from pipelines.paper_trading_pipeline import run_paper_trading_from_latest
from pipelines.replay_audit_ledger import (
    list_replay_audit_dates,
    list_replay_audit_runs,
    load_replay_audit_day,
    load_replay_audit_markdown,
)
from pipelines.schemas import PipelineStatus
from portfolio.decision_attribution import (
    explain_stock_decision_attribution,
    render_decision_attribution_markdown,
)
from portfolio.paper_account import (
    cancel_pending_paper_cash_flow,
    list_daily_order_snapshot_dates,
    list_daily_position_snapshot_dates,
    load_daily_order_snapshot,
    load_daily_position_snapshot,
    load_paper_cash_flows,
    load_paper_trading_snapshot,
)
from portfolio.trading_permissions import (
    DEFAULT_TRADING_PERMISSIONS,
    TRADING_PERMISSION_LABELS,
    format_permission_summary,
    normalize_trading_permissions,
)

from application.paper_profile_service import (
    get_classic_user_profile_form_options,
    has_required_paper_trading_profile,
    load_classic_user_context,
    save_classic_user_context,
)
from database.repositories import PredictionRepository


class PaperTradingApplicationService:
    """Application boundary for every AI paper-trading UI operation."""

    @staticmethod
    def path_cache_version(path: str | Path) -> tuple[str, int, int]:
        resolved = Path(path)
        try:
            stat = resolved.stat()
            size = stat.st_size if resolved.is_file() else 0
            return str(resolved), int(stat.st_mtime_ns), int(size)
        except OSError:
            return str(resolved), 0, 0

    def paper_cache_versions(
        self,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None = None,
    ) -> tuple[tuple[str, int, int], ...]:
        if db_path:
            return (self.path_cache_version(db_path),)
        root = Path(output_dir) / "portfolio" / str(user_id)
        paths = [
            root / "paper_account_latest.json",
            root / "paper_account.json",
            root / "paper_positions_latest.csv",
            root / "paper_positions.csv",
            root / "paper_orders_latest.csv",
            root / "paper_orders.csv",
            root / "paper_nav_latest.csv",
            root / "portfolio_risk_report_latest.json",
            root / "portfolio_risk_report.json",
            root / "ai_paper_decisions_latest.json",
            root / "paper_execution_diagnostics_latest.json",
            root / "paper_trading_settings.json",
            root / "history" / "orders",
            root / "history" / "positions",
        ]
        return tuple(self.path_cache_version(path) for path in paths)

    def ai_reliability_cache_version(
        self, user_id: str, output_dir: str | Path
    ) -> tuple[str, int, int]:
        path = Path(output_dir) / "portfolio" / str(user_id) / "ai_reliability_state.json"
        return self.path_cache_version(path)

    def cash_flow_cache_versions(
        self,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None = None,
    ) -> tuple[tuple[str, int, int], ...]:
        if db_path:
            return (self.path_cache_version(db_path),)
        root = Path(output_dir) / "portfolio" / str(user_id)
        return (
            self.path_cache_version(root / "cash_flows.json"),
            self.path_cache_version(root / "paper_cash_flows.json"),
        )

    def daily_order_cache_versions(
        self, user_id: str, trade_date: str, output_dir: str | Path
    ) -> tuple[tuple[str, int, int], ...]:
        root = Path(output_dir) / "portfolio" / str(user_id)
        token = "".join(ch for ch in str(trade_date) if ch.isdigit())[:8]
        return (
            self.path_cache_version(root / "history" / "orders"),
            self.path_cache_version(root / "history" / "orders" / f"orders_{token}.csv"),
            self.path_cache_version(root / "paper_orders_latest.csv"),
        )

    def daily_position_cache_versions(
        self, user_id: str, trade_date: str, output_dir: str | Path
    ) -> tuple[tuple[str, int, int], ...]:
        root = Path(output_dir) / "portfolio" / str(user_id)
        token = "".join(ch for ch in str(trade_date) if ch.isdigit())[:8]
        return (
            self.path_cache_version(root / "history" / "positions"),
            self.path_cache_version(root / "history" / "positions" / f"positions_{token}.csv"),
            self.path_cache_version(root / "paper_positions_latest.csv"),
        )

    def load_latest_ranking(
        self,
        output_dir: str | Path,
        db_path: str | Path | None = None,
    ) -> pd.DataFrame:
        del output_dir
        return pd.DataFrame(
            PredictionRepository(db_path).list_latest_predictions()
        )

    def ranking_exists(
        self,
        output_dir: str | Path,
        db_path: str | Path | None = None,
    ) -> bool:
        return not self.load_latest_ranking(output_dir, db_path).empty

    @staticmethod
    def execute_tool(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(_execute_tool(*args, **kwargs) or {})

    @staticmethod
    def execute_confirmed_plan_v2(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(_execute_confirmed_plan_v2(*args, **kwargs) or {})


paper_trading_service = PaperTradingApplicationService()


def execute_tool(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return paper_trading_service.execute_tool(*args, **kwargs)


def execute_confirmed_plan_v2(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return paper_trading_service.execute_confirmed_plan_v2(*args, **kwargs)


def path_cache_version(path: str | Path) -> tuple[str, int, int]:
    return paper_trading_service.path_cache_version(path)


def paper_cache_versions(
    user_id: str, output_dir: str | Path, db_path: str | Path | None = None
) -> tuple[tuple[str, int, int], ...]:
    return paper_trading_service.paper_cache_versions(user_id, output_dir, db_path)


def ai_reliability_cache_version(
    user_id: str, output_dir: str | Path
) -> tuple[str, int, int]:
    return paper_trading_service.ai_reliability_cache_version(user_id, output_dir)


def cash_flow_cache_versions(
    user_id: str, output_dir: str | Path, db_path: str | Path | None = None
) -> tuple[tuple[str, int, int], ...]:
    return paper_trading_service.cash_flow_cache_versions(user_id, output_dir, db_path)


def daily_order_cache_versions(
    user_id: str, trade_date: str, output_dir: str | Path
) -> tuple[tuple[str, int, int], ...]:
    return paper_trading_service.daily_order_cache_versions(user_id, trade_date, output_dir)


def daily_position_cache_versions(
    user_id: str, trade_date: str, output_dir: str | Path
) -> tuple[tuple[str, int, int], ...]:
    return paper_trading_service.daily_position_cache_versions(user_id, trade_date, output_dir)


def load_latest_ranking(
    output_dir: str | Path,
    db_path: str | Path | None = None,
) -> pd.DataFrame:
    return paper_trading_service.load_latest_ranking(output_dir, db_path)


def ranking_exists(
    output_dir: str | Path,
    db_path: str | Path | None = None,
) -> bool:
    return paper_trading_service.ranking_exists(output_dir, db_path)


__all__ = [
    "AGENT_MAIN",
    "DEFAULT_INITIAL_CASH",
    "DEFAULT_PAPER_TRADING_START_DATE",
    "DEFAULT_TRADING_PERMISSIONS",
    "PipelineStatus",
    "TRADING_PERMISSION_LABELS",
    "PaperTradingApplicationService",
    "cancel_pending_paper_cash_flow",
    "path_cache_version",
    "paper_cache_versions",
    "load_latest_ranking",
    "daily_position_cache_versions",
    "daily_order_cache_versions",
    "cash_flow_cache_versions",
    "ai_reliability_cache_version",
    "execute_confirmed_plan_v2",
    "execute_tool",
    "explain_stock_decision_attribution",
    "format_permission_summary",
    "get_classic_user_profile_form_options",
    "has_required_paper_trading_profile",
    "list_daily_order_snapshot_dates",
    "list_daily_position_snapshot_dates",
    "list_replay_audit_dates",
    "list_replay_audit_runs",
    "load_ai_reliability_state",
    "load_classic_user_context",
    "load_daily_order_snapshot",
    "load_daily_position_snapshot",
    "load_paper_backfill_status",
    "load_paper_cash_flows",
    "load_paper_trading_snapshot",
    "load_replay_audit_day",
    "load_replay_audit_markdown",
    "normalize_trading_permissions",
    "ranking_exists",
    "render_decision_attribution_markdown",
    "run_paper_trading_from_latest",
    "save_classic_user_context",
    "sync_event_cache_to_agent_db",
]
