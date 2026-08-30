from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from database.connection import verify_database
from database.postgres_config import PostgresSettings
from scheduler.job_state import load_latest_job_status
from scheduler.trading_calendar import get_latest_trading_day, is_trading_day


def run_health_check(root: str | Path = ".", db_path=None) -> dict[str, Any]:
    del db_path
    root_path = Path(root)
    checks: dict[str, Any] = {
        "python": sys.executable,
        "root": str(root_path.resolve()),
        "api_entry_exists": (root_path / "run_agent_api.py").exists(),
        "daily_update_exists": (root_path / "daily_incremental_update.py").exists(),
        "scheduler_package_exists": (root_path / "scheduler").exists(),
        "run_script_exists": (root_path / "scripts" / "run_scheduled_daily_update.bat").exists(),
        "latest_status_exists": bool(load_latest_job_status(root_path)),
    }
    try:
        settings = PostgresSettings.from_env()
        verify_database()
        checks["database_ok"] = True
        checks["database_backend"] = "postgresql"
        checks["database_name"] = settings.database
        checks["database_schema"] = settings.app_schema
    except Exception as exc:
        checks["database_ok"] = False
        checks["database_error"] = str(exc)
    try:
        latest = get_latest_trading_day(None)
        checks["calendar_ok"] = True
        checks["latest_trading_day"] = latest.strftime("%Y-%m-%d")
        checks["today_is_trading_day"] = is_trading_day(None)
    except Exception as exc:
        checks["calendar_ok"] = False
        checks["calendar_error"] = str(exc)
    checks["overall_status"] = "success" if checks.get("api_entry_exists") and checks.get("daily_update_exists") and checks.get("database_ok") and checks.get("calendar_ok") else "failed"
    return checks
