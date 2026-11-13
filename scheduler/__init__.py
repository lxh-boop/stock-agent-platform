"""轻量调度接口；读取计划/状态时不提前加载模型与 Agent 引擎。"""
from __future__ import annotations
from importlib import import_module

_EXPORTS = {
    "get_latest_trading_day": "scheduler.trading_calendar",
    "get_next_trading_day": "scheduler.trading_calendar",
    "is_trading_day": "scheduler.trading_calendar",
    "load_latest_job_status": "scheduler.job_state",
    "run_health_check": "scheduler.health_check",
    "run_scheduled_daily_update": "scheduler.daily_worker",
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(_EXPORTS[name]), name)
    globals()[name] = value
    return value
