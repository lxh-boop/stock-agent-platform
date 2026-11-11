"""个人计划持久化在 user_account；统一 ticker 只负责提交用户隔离的 Task。"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, time
from typing import Any
from zoneinfo import ZoneInfo

from core.user_scope import user_scope, workspace
from database.repositories.auth_repository import AuthRepository
from scheduler.trading_calendar import get_latest_trading_day, is_trading_day

LOGGER = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")
TICK_ID = "account_daily_dispatcher"


def normalize_schedule(value: dict[str, Any]) -> dict[str, Any]:
    hour = int(value.get("auto_retrain_hour", 20))
    minute = int(value.get("auto_retrain_minute", 0))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("invalid_scheduler_time")
    return {"auto_retrain_enabled": bool(value.get("auto_retrain_enabled", False)),
            "auto_retrain_hour": hour, "auto_retrain_minute": minute,
            "auto_retrain_timezone": "Asia/Shanghai",
            "auto_retrain_catch_up": bool(value.get("auto_retrain_catch_up", True))}


def completed_signal_date(now: datetime | None = None) -> str:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    day = now.date()
    if is_trading_day(day) and now.time().replace(tzinfo=None) < time(15, 30):
        day -= timedelta(days=1)
    return str(get_latest_trading_day(day))


def due_slot(config: dict[str, Any], now: datetime) -> str:
    cfg = normalize_schedule(config)
    if not cfg["auto_retrain_enabled"]:
        return ""
    now = now.astimezone(TZ)
    planned = now.replace(hour=cfg["auto_retrain_hour"], minute=cfg["auto_retrain_minute"], second=0, microsecond=0)
    if now < planned:
        planned -= timedelta(days=1)
    planned_day = get_latest_trading_day(planned.date())
    planned = planned.replace(year=planned_day.year, month=planned_day.month, day=planned_day.day)
    if not cfg["auto_retrain_catch_up"] and (now - planned).total_seconds() >= 60:
        return ""
    # 对同一计划时刻最多提交一次；失败不每 30 秒无限自动重试。
    return planned.isoformat(timespec="minutes")


def dispatch_due_accounts(now: datetime | None = None) -> None:
    from application.user_daily_service import submit_personal_daily
    now = now or datetime.now(TZ)
    for row in AuthRepository().list_scheduled_accounts():
        user_id = str(row["user_id"])
        try:
            config = json.loads(row.get("scheduler_config_json") or "{}")
            slot = due_slot(config, now)
            if slot:
                submit_personal_daily(user_id, source="scheduled", slot=slot, force=False)
        except Exception as exc:
            # 不把凭据或数据库连接串写到公共日志。
            LOGGER.warning("Account daily submission failed user=%s type=%s", user_id, type(exc).__name__)


def register_dispatcher(scheduler: Any) -> None:
    if scheduler.get_job(TICK_ID):
        return
    scheduler.add_job(dispatch_due_accounts, trigger="interval", seconds=30,
                      next_run_time=datetime.now(TZ) + timedelta(seconds=5),
                      id=TICK_ID, replace_existing=True, max_instances=1,
                      coalesce=True, misfire_grace_time=60)


def public_status(user_id: str) -> dict[str, Any]:
    from scheduler import runtime_scheduler as runtime
    from scheduler.job_state import load_latest_job_status
    from server.task_runtime.store import TaskStore
    from application.user_daily_service import DAILY_TASK_TYPE

    cfg = normalize_schedule(AuthRepository().scheduler_settings(user_id))
    now = datetime.now(TZ)
    plan = now.replace(hour=cfg["auto_retrain_hour"], minute=cfg["auto_retrain_minute"], second=0, microsecond=0)
    while plan <= now or not is_trading_day(plan.date()):
        plan += timedelta(days=1)
    with user_scope(user_id):
        rows = TaskStore().list(owner_id=user_id, task_type=DAILY_TASK_TYPE, limit=1)
        task = rows[0] if rows else {}
        latest = load_latest_job_status(workspace(user_id))
        signal_status, signal_error = "available", ""
        try:
            signal = runtime.read_ranking_signal_date()
            if not signal:
                signal_status = "empty"
        except Exception as exc:
            signal, signal_status, signal_error = "", "unavailable", type(exc).__name__
    expected = completed_signal_date(now)
    failed_step, error_type, reason = runtime._latest_failure_details(latest)
    task_error = task.get("error") or {}
    if isinstance(task_error, dict) and task_error.get("message"):
        reason = runtime._safe_scheduler_error(str(task_error["message"]))
        error_type = str(task_error.get("code") or error_type)
    status = str(task.get("status") or latest.get("overall_status") or "unknown")
    status = {"succeeded": "success", "queued": "pending"}.get(status, status)
    if status in {"running", "pending", "success"}:
        reason, error_type, failed_step = "", "", ""
    scheduler = runtime._SCHEDULER
    running = bool(scheduler and scheduler.running)
    return {
        "scope": "account", "owner_id": user_id,
        "enabled": cfg["auto_retrain_enabled"], "hour": cfg["auto_retrain_hour"],
        "minute": cfg["auto_retrain_minute"], "timezone": "Asia/Shanghai",
        "catch_up": cfg["auto_retrain_catch_up"], "runtime_running": running,
        "job_registered": bool(running and scheduler.get_job(TICK_ID) and cfg["auto_retrain_enabled"]),
        "next_run_time": plan.isoformat(timespec="seconds") if cfg["auto_retrain_enabled"] else "",
        "expected_signal_date": expected, "latest_signal_date": signal,
        "ranking_signal_status": signal_status, "ranking_signal_error": signal_error,
        "stale": signal != expected, "market_stale": signal != expected,
        "last_started_at": str(task.get("started_at") or latest.get("started_at") or ""),
        "last_finished_at": str(task.get("finished_at") or latest.get("finished_at") or ""),
        "last_trade_date": str(latest.get("trade_date") or ""),
        "last_status": status, "current_step": str(latest.get("current_step") or ""),
        "last_error": reason, "last_failed_step": failed_step, "last_error_type": error_type,
    }
