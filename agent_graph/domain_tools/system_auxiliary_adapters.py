from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.python_sandbox_service import python_sandbox_service
from agent_graph.services.system_auxiliary_service import system_auxiliary_service
from agent_graph.services.user_profile_service import user_profile_service


def _context_value(args: dict[str, Any], context: dict[str, Any], key: str, default: Any = None) -> Any:
    value = args.get(key)
    if value not in (None, ""):
        return value
    value = context.get(key)
    return default if value in (None, "") else value


def _output_dir(context: dict[str, Any]) -> str | Path:
    return context.get("output_dir") or "outputs"


def _db_path(context: dict[str, Any]) -> str | Path | None:
    return context.get("db_path")


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def user_profile_get_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return user_profile_service.get_user_profile(
        str(_context_value(args, context, "user_id", "default")),
        output_dir=_output_dir(context),
        db_path=_db_path(context),
    )


def python_sandbox_analysis_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return python_sandbox_service.run_analysis(
        str(args.get("code") or ""),
        snapshot=dict(args.get("snapshot") or {}),
        snapshot_id=str(args.get("snapshot_id") or ""),
        timeout_seconds=_float_value(args.get("timeout_seconds"), 995.0),
        max_output_chars=_int_value(args.get("max_output_chars"), 4000),
    )


def scheduler_status_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return system_auxiliary_service.scheduler_status(
        root=args.get("root") or context.get("root") or ".",
    )


def report_list_latest_adapter(args: dict[str, Any], context: dict[str, Any]) -> Any:
    return system_auxiliary_service.list_latest_reports(
        output_dir=args.get("output_dir") or _output_dir(context),
    )
