from __future__ import annotations

import os
import time
from typing import Any, Callable


Emit = Callable[[str, dict[str, Any]], None]
Cancelled = Callable[[], bool]


def _llm_settings_from_descriptor(descriptor: dict[str, Any] | None, secrets: dict[str, str]) -> Any:
    if not descriptor:
        return None
    from core.llm.runtime_settings import resolve_active_llm_settings

    mode = str(descriptor.get("mode") or "").strip().lower()
    if mode not in {"api", "local"}:
        raise RuntimeError("invalid_llm_settings_descriptor_mode")

    config: dict[str, Any] = {
        "llm_mode": mode,
        "llm_request_timeout_seconds": descriptor.get("request_timeout_seconds", 99120),
        "llm_max_retries": descriptor.get("max_retries", 0),
    }
    if mode == "api":
        config.update(
            {
                "llm_api_profile_id": descriptor.get("profile_id"),
                "llm_api_provider": descriptor.get("provider"),
                "llm_api_base_url": descriptor.get("base_url"),
                "llm_api_model": descriptor.get("model"),
                "llm_api_disable_thinking": descriptor.get("disable_thinking", False),
                "llm_api_context_window": descriptor.get("context_window", 128000),
                "llm_api_supports_json_schema": descriptor.get("supports_json_schema", True),
                "llm_api_supports_tools": descriptor.get("supports_tools", True),
            }
        )
    else:
        config.update(
            {
                "llm_local_profile_id": descriptor.get("profile_id"),
                "llm_local_base_url": descriptor.get("base_url"),
                "llm_local_model": descriptor.get("model"),
                "llm_local_disable_thinking": descriptor.get("disable_thinking", False),
                "llm_local_context_window": descriptor.get("context_window", 32768),
                "llm_local_supports_json_schema": descriptor.get("supports_json_schema", False),
                "llm_local_supports_tools": descriptor.get("supports_tools", False),
            }
        )

    settings = resolve_active_llm_settings(
        local_config=config,
        profile_id=str(descriptor.get("profile_id") or "") or None,
        mode=mode,
        api_key=secrets.get("llm_credential") or None,
        base_url=str(descriptor.get("base_url") or "") or None,
        model=str(descriptor.get("model") or "") or None,
    )

    expected = {
        "mode": mode,
        "provider": str(descriptor.get("provider") or ""),
        "model": str(descriptor.get("model") or ""),
        "profile_id": str(descriptor.get("profile_id") or ""),
    }
    actual = {
        "mode": str(settings.mode),
        "provider": str(settings.provider),
        "model": str(settings.model),
        "profile_id": str(settings.profile_id),
    }
    mismatches = [
        key
        for key, expected_value in expected.items()
        if expected_value and actual.get(key) != expected_value
    ]
    if mismatches:
        raise RuntimeError(
            "llm_settings_descriptor_mismatch:"
            + ",".join(sorted(mismatches))
        )
    return settings


def execute_task(
    task_type: str,
    request: dict[str, Any],
    *,
    emit: Emit,
    is_cancelled: Cancelled,
    secrets: dict[str, str] | None = None,
    attempt: int = 0,
) -> Any:
    args = list(request.get("args") or [])
    kwargs = dict(request.get("kwargs") or {})
    secrets = dict(secrets or {})

    if task_type == "diagnostic.flaky":
        fail_attempts = max(0, int(kwargs.get("fail_attempts", 1)))
        emit("progress", {"progress": min(0.8, 0.2 + 0.2 * attempt), "message": f"诊断重试任务，第 {attempt + 1} 次执行"})
        if attempt < fail_attempts:
            raise RuntimeError(f"Intentional diagnostic failure at attempt {attempt}")
        return {"attempt": attempt, "recovered": True}

    if task_type == "diagnostic.sleep":
        seconds = max(0.1, float(kwargs.get("seconds", 3)))
        steps = max(1, int(kwargs.get("steps", 6)))
        for index in range(steps):
            if is_cancelled():
                raise InterruptedError("Task cancellation requested")
            time.sleep(seconds / steps)
            emit("progress", {"progress": (index + 1) / steps, "message": f"诊断任务步骤 {index + 1}/{steps}"})
        return {"slept_seconds": seconds, "steps": steps}

    if task_type == "agent.run":
        from application.agent_service import AgentApplicationService

        db_path = kwargs.pop("_service_db_path", None)
        descriptor = kwargs.pop("llm_settings_descriptor", None)
        kwargs["llm_settings"] = _llm_settings_from_descriptor(descriptor, secrets)
        emit("stage", {"progress": 0.08, "message": "Agent 正在读取上下文并规划任务"})
        result = AgentApplicationService(db_path).run(*args, **kwargs)
        emit("stage", {"progress": 0.95, "message": "Agent 已完成工具执行，正在整理结果"})
        return result

    if task_type == "dashboard.rolling_update":
        from application.dashboard_service import dashboard_service

        estimate = max(30, int(kwargs.pop("estimated_seconds", 300)))
        emit("stage", {"progress": 0.03, "message": "正在启动每日滚动更新"})
        job = dashboard_service.start_rolling_update_job(**kwargs)
        start = time.monotonic()
        try:
            while job.poll() is None:
                if is_cancelled():
                    job.kill()
                    raise InterruptedError("Task cancellation requested")
                elapsed = time.monotonic() - start
                progress = min(0.92, max(0.04, elapsed / estimate * 0.92))
                emit("progress", {"progress": progress, "message": "每日更新正在运行", "elapsed_seconds": round(elapsed, 1)})
                time.sleep(1)
            returncode = job.returncode
            if returncode != 0:
                raise RuntimeError(f"Rolling update process failed with return code {returncode}")
            return {"returncode": returncode, "log_path": job.log_path, "masked_command": list(job.masked_command or [])}
        finally:
            try:
                job.close()
            except Exception:
                pass

    if task_type == "dashboard.backtest":
        from application.dashboard_service import run_latest_t1_backtest

        emit("stage", {"progress": 0.05, "message": "正在检查行情并运行 T+1 回测"})
        result = run_latest_t1_backtest(*args, **kwargs)
        emit("stage", {"progress": 0.95, "message": "回测已完成，正在保存结果"})
        return result

    if task_type == "paper-trading.update":
        from application.paper_trading_service import (
            run_paper_trading_from_latest,
            sync_event_cache_to_agent_db,
        )

        sync_kwargs = dict(kwargs.pop("sync_kwargs", {}) or {})
        emit("stage", {"progress": 0.08, "message": "正在同步新闻与公告事件"})
        sync_event_cache_to_agent_db(**sync_kwargs)
        if is_cancelled():
            raise InterruptedError("Task cancellation requested")
        emit("stage", {"progress": 0.35, "message": "正在生成模拟盘组合并执行规则校验"})
        result = run_paper_trading_from_latest(*args, **kwargs)
        emit("stage", {"progress": 0.95, "message": "模拟盘更新完成，正在刷新快照"})
        return result

    if task_type == "paper-trading.backfill":
        from application.web_paper_trading_service import web_paper_trading_service

        emit("stage", {"progress": 0.05, "message": "正在重新校验回填预案"})
        if is_cancelled():
            raise InterruptedError("Task cancellation requested")
        emit("stage", {"progress": 0.12, "message": "正在执行历史模拟盘回填"})
        result = web_paper_trading_service.commit_proposal(allow_long_running=True, **kwargs)
        emit("stage", {"progress": 0.97, "message": "回填完成，正在刷新模拟盘快照"})
        return result

    if task_type == "paper-profile.ai-news-adjustment":
        from application.paper_profile_service import run_ai_news_adjustment_from_latest

        emit("stage", {"progress": 0.08, "message": "正在执行新闻调整"})
        return run_ai_news_adjustment_from_latest(*args, **kwargs)

    if task_type == "paper-profile.scheduler-manual":
        emit("stage", {"progress": 0.08, "message": "正在执行调度器手动任务"})
        from core.user_scope import current_owner
        from application.user_daily_service import run_personal_daily
        return run_personal_daily(current_owner(), force=bool(kwargs.get("force", True)),
                                  source=str(kwargs.get("source") or "manual"))

    raise KeyError(f"Unsupported task type: {task_type}")
