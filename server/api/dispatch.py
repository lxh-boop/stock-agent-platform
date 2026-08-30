from __future__ import annotations

import threading
import traceback
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable

from server.api.serialization import decode_transport, encode_transport


DASHBOARD_FUNCTIONS = {
    "build_display_date_options",
    "classify_event_title",
    "build_mcp_context_from_local_config",
    "calculate_topk_rebalance",
    "build_stock_explanation_prompt",
    "create_project_model",
    "discover_mcp_tools",
    "downloaded_zoo_backends",
    "ensure_runtime_directories",
    "explain_prompt_with_llm",
    "get_ollama_version",
    "is_frozen_app",
    "is_prediction_only_date",
    "is_zoo_backend",
    "list_model_names",
    "list_local_models",
    "load_cached_ai_explanation",
    "load_daily_returns_for_strategy",
    "load_local_config",
    "load_selected_strategy",
    "mcp_sdk_version",
    "pull_model",
    "read_auto_retrain_log",
    "registered_zoo_backends",
    "reset_discovery_cache",
    "resolve_active_llm_settings",
    "save_local_config",
    "validate_local_model",
    "validate_tushare_token",
    "zoo_model_name_from_backend",
}

DASHBOARD_METHODS = {
    "path_cache_version",
    "load_event_cache",
    "retrieve_stock_context",
    "load_time_estimate",
    "save_time_cost",
    "read_log_tail",
    "get_ranking_file_snapshot",
    "load_ranking",
    "load_metrics",
    "load_json_file",
    "load_backtest_outputs",
    "load_model_zoo_table",
    "load_external_backtest_summary",
    "load_external_daily_returns",
    "load_news_events_for_app",
    "load_external_model_status",
    "run_external_backend_ranking",
    "test_neo4j_connection",
    "inspect_model",
    "load_latest_raw_data",
    "rag_ready",
    "file_status_rows",
}

DASHBOARD_CONSTANTS = {
    "ANNOUNCEMENT_CACHE_PATH",
    "A_SHARE_DAILY_DATA_READY_TIME",
    "BACKTEST_DAILY_PREDICTIONS_PATH",
    "BACKTEST_DISCLAIMER",
    "BACKTEST_MASTER_TABLE_PATH",
    "BACKTEST_METRICS_PATH",
    "BACKTEST_NAV_PATH",
    "BACKTEST_TRADES_PATH",
    "DEFAULT_DFT_UNET_CHECKPOINT_PATH",
    "BASE_DIR",
    "DEFAULT_LLM_BASE_URL",
    "DEFAULT_LLM_MODEL",
    "ENABLE_LLM_EXPLAINER",
    "ENABLE_NEWS_FEATURES",
    "ENABLE_RAG",
    "LATEST_RAW_DATA_PATH",
    "LLM_API_KEY_ENV",
    "LLM_BASE_URL_ENV",
    "LLM_MODEL_ENV",
    "LOG_DIR",
    "MARKET_CONTEXT_FEATURE_CACHE_PATH",
    "MARKET_CONTEXT_INDEX_DAILY_CACHE_PATH",
    "METRICS_PATH",
    "MODEL_CANDIDATES_PATH",
    "MODEL_SEARCH_RESULTS_PATH",
    "NEWS_CACHE_PATH",
    "OLLAMA_PROJECT_MODEL",
    "OLLAMA_PROJECT_MODELFILE_NAME",
    "OUTPUT_DIR",
    "PROGRESS_HISTORY_PATH",
    "RAG_DOCUMENTS_PATH",
    "RAG_INDEX_PATH",
    "RANKING_LATEST_PATH",
    "RECOMMENDED_BASE_MODEL",
    "ROLLING_UPDATE_LOG_PATH",
    "ROLLING_UPDATE_SCRIPT",
    "RUN_CWD",
    "SELECTED_STRATEGY_PATH",
    "UNIVERSE",
}

AGENT_UTILITY_FUNCTIONS = {
    "build_handoff_safe_summary",
    "build_memory_safe_summary",
    "build_react_safe_summary",
    "build_reflection_safe_summary",
    "format_handoff_caption",
    "format_reflection_caption",
    "list_memory_records_safe_page",
    "list_safe_observation_summaries",
    "load_run_snapshot",
    "summarize_mcp_usage",
}

AGENT_CONSTANTS = {"PAPER_AGENT_DISCLAIMER"}

AGENT_SERVICE_METHODS = {
    "list_active_conversations",
    "list_recent_messages",
    "get_conversation",
    "get_user_conversation",
    "upsert_conversation",
    "rename_conversation",
    "soft_delete_conversation",
    "upsert_message",
    "list_agent_runs_by_ids",
    "run",
    "list_registered_tools",
    "list_pending_actions",
    "query_portfolio",
    "query_scheduler",
    "control_action",
    "build_message_trace_summary",
}

STRATEGY_PROPOSAL_METHODS = {
    "get_active",
    "list_versions",
    "create_proposal",
    "create_version",
    "update_status",
}

PAPER_FUNCTIONS = {
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
    "read_csv",
    "render_decision_attribution_markdown",
    "save_classic_user_context",
    "sync_event_cache_to_agent_db",
}

PAPER_PROFILE_FUNCTIONS = {
    "portfolio_output_dir",
    "user_output_dir",
    "format_classic_ranking_for_display",
    "load_classic_ranking_with_ai_adjustment",
    "build_ai_adjustment_detail",
    "load_current_ai_reliability_state",
    "load_scheduler_status_summary",
    "read_scheduler_log_tail",
    "has_required_paper_trading_profile",
    "save_classic_user_context",
    "load_classic_user_context",
    "cancel_pending_paper_cash_flow",
    "get_classic_user_profile_form_options",
}

PAPER_CONSTANTS = {
    "AGENT_MAIN",
    "DEFAULT_INITIAL_CASH",
    "DEFAULT_PAPER_TRADING_START_DATE",
    "DEFAULT_TRADING_PERMISSIONS",
    "TRADING_PERMISSION_LABELS",
}

MODEL_FUNCTIONS = {
    "format_strategy_option",
    "load_daily_returns_for_strategy",
    "load_model_discovery_report",
    "load_selected_strategy",
    "load_table_file",
    "make_strategy_from_row",
    "resolve_output_path",
    "save_selected_strategy",
}

MODEL_CONSTANTS = {
    "BACKTEST_DISCLAIMER",
    "BACKTEST_MASTER_TABLE_PATH",
    "MODEL_CANDIDATES_PATH",
    "MODEL_DISCOVERY_REPORT_PATH",
    "MODEL_SEARCH_ERRORS_PATH",
    "MODEL_SEARCH_RESULTS_PATH",
    "SELECTED_STRATEGY_PATH",
}

MONITOR_FUNCTIONS = {
    "build_handoff_health_summary",
    "build_memory_store_health_summary",
    "build_message_bus_health_summary",
    "build_react_health_summary",
    "build_reflection_health_summary",
    "build_system_monitor_snapshot",
    "collect_and_store_system_monitor_snapshot",
    "list_system_monitor_alerts",
    "list_system_monitor_history",
}

HANDOFF_FUNCTIONS = {
    "build_handoff_safe_summary",
    "format_handoff_caption",
    "build_handoff_health_summary",
}

REFLECTION_FUNCTIONS = {
    "build_reflection_safe_summary",
    "format_reflection_caption",
    "build_reflection_health_summary",
}


class LLMSettingsRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, Any] = {}

    def register(self, settings: Any) -> dict[str, Any]:
        token = uuid.uuid4().hex
        with self._lock:
            self._items[token] = settings
        return {
            "settings_token": token,
            "profile_id": str(settings.profile_id),
            "mode": str(settings.mode),
            "provider": str(settings.provider),
            "base_url": str(settings.base_url),
            "model": str(settings.model),
            "disable_thinking": bool(settings.disable_thinking),
            "request_timeout_seconds": int(settings.request_timeout_seconds),
            "max_retries": int(settings.max_retries),
            "endpoint_scope": str(settings.endpoint_scope),
            "is_configured": bool(settings.is_configured),
        }

    def resolve_current(self) -> Any:
        """Resolve the latest persisted profile for a newly submitted task."""

        from core.llm.runtime_settings import resolve_active_llm_settings

        return resolve_active_llm_settings()

    def resolve(self, payload: Any) -> Any:
        from core.llm.runtime_settings import resolve_active_llm_settings

        if not isinstance(payload, dict):
            return payload
        token = str(payload.get("settings_token") or "")
        if token:
            with self._lock:
                settings = self._items.get(token)
            if settings is not None:
                return settings
        profile_id = str(payload.get("profile_id") or "")
        mode = str(payload.get("mode") or "")
        return resolve_active_llm_settings(
            profile_id=profile_id or None,
            mode=mode or None,
        )


llm_settings_registry = LLMSettingsRegistry()
_scheduler_lock = threading.RLock()
_scheduler: Any = None


def _scheduler_instance() -> Any:
    global _scheduler
    from application.dashboard_service import create_scheduler

    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = create_scheduler()
        return _scheduler


def dashboard_bootstrap() -> dict[str, Any]:
    import application.dashboard_service as module

    return {name: getattr(module, name) for name in sorted(DASHBOARD_CONSTANTS)}


def agent_bootstrap() -> dict[str, Any]:
    import application.agent_service as module

    return {name: getattr(module, name) for name in sorted(AGENT_CONSTANTS)}


def paper_bootstrap() -> dict[str, Any]:
    import application.paper_trading_service as module

    payload = {name: getattr(module, name) for name in sorted(PAPER_CONSTANTS)}
    payload["PipelineStatus"] = {
        "SUCCESS": "success",
        "FAILED": "failed",
        "SKIPPED": "skipped",
        "PARTIAL": "partial",
    }
    return payload


def model_bootstrap() -> dict[str, Any]:
    import application.model_search_service as module

    return {name: getattr(module, name) for name in sorted(MODEL_CONSTANTS)}


def _invoke_callable(callable_obj: Callable[..., Any], args: list[Any], kwargs: dict[str, Any]) -> Any:
    return callable_obj(*args, **kwargs)


def invoke_dashboard(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    import application.dashboard_service as module

    if operation == "resolve_active_llm_settings":
        settings = module.resolve_active_llm_settings(*args, **kwargs)
        return llm_settings_registry.register(settings)
    if operation == "validate_llm_connection":
        from core.llm import LLMService

        settings_payload = kwargs.get("settings") or (args[0] if args else {})
        settings = llm_settings_registry.resolve(settings_payload)
        return LLMService(settings).validate_connection()
    if operation == "explain_prompt_with_llm":
        kwargs = dict(kwargs)
        if isinstance(kwargs.get("llm_settings"), dict):
            kwargs["llm_settings"] = llm_settings_registry.resolve(kwargs["llm_settings"])
        return module.explain_prompt_with_llm(*args, **kwargs)
    if operation in DASHBOARD_METHODS:
        return _invoke_callable(getattr(module.dashboard_service, operation), args, kwargs)
    if operation in DASHBOARD_FUNCTIONS:
        return _invoke_callable(getattr(module, operation), args, kwargs)
    if operation == "create_scheduler":
        scheduler = _scheduler_instance()
        return {"scheduler_id": "default", "running": bool(getattr(scheduler, "running", True))}
    if operation == "get_scheduler_jobs":
        return module.get_scheduler_jobs(_scheduler_instance())
    if operation == "set_daily_retrain_job":
        kwargs = dict(kwargs)
        kwargs.pop("scheduler", None)
        return module.set_daily_retrain_job(scheduler=_scheduler_instance(), **kwargs)
    raise KeyError(f"Dashboard operation is not allowed: {operation}")


def invoke_agent(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    import application.agent_service as module

    if operation.startswith("service."):
        method = operation.split(".", 1)[1]
        if method not in AGENT_SERVICE_METHODS:
            raise KeyError(f"Agent service method is not allowed: {method}")
        db_path = kwargs.pop("_service_db_path", None)
        service = module.AgentApplicationService(db_path)
        if method == "run" and isinstance(kwargs.get("llm_settings"), dict):
            kwargs["llm_settings"] = llm_settings_registry.resolve(kwargs["llm_settings"])
        return _invoke_callable(getattr(service, method), args, kwargs)
    if operation.startswith("strategy_proposal."):
        method = operation.split(".", 1)[1]
        if method not in STRATEGY_PROPOSAL_METHODS:
            raise KeyError(f"Strategy proposal method is not allowed: {method}")
        db_path = kwargs.pop("_service_db_path", None)
        service = module.StrategyProposalService(db_path)
        return _invoke_callable(getattr(service, method), args, kwargs)
    if operation in AGENT_UTILITY_FUNCTIONS:
        return _invoke_callable(getattr(module, operation), args, kwargs)
    if operation == "trace_event":
        return module.trace_event(*args, **kwargs)
    if operation == "trace_exception":
        event = str(kwargs.pop("event", args[0] if args else "ui.remote.error"))
        message = str(kwargs.pop("message", args[1] if len(args) > 1 else "Remote client exception"))
        exc = RuntimeError(message)
        return module.trace_exception(event, exc, **kwargs)
    raise KeyError(f"Agent operation is not allowed: {operation}")


def invoke_paper(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    import application.paper_trading_service as module

    if operation not in PAPER_FUNCTIONS:
        raise KeyError(f"Paper-trading operation is not allowed: {operation}")
    return _invoke_callable(getattr(module, operation), args, kwargs)


def invoke_paper_profile(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    import application.paper_profile_service as module

    if operation not in PAPER_PROFILE_FUNCTIONS:
        raise KeyError(f"Paper-profile operation is not allowed: {operation}")
    return _invoke_callable(getattr(module, operation), args, kwargs)


def invoke_model(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    import application.model_search_service as module

    if operation not in MODEL_FUNCTIONS:
        raise KeyError(f"Model-search operation is not allowed: {operation}")
    return _invoke_callable(getattr(module, operation), args, kwargs)


def invoke_monitor(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    import application.system_monitor_service as module

    if operation not in MONITOR_FUNCTIONS:
        raise KeyError(f"System-monitor operation is not allowed: {operation}")
    return _invoke_callable(getattr(module, operation), args, kwargs)


def invoke_handoff(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    import application.handoff_service as module

    if operation not in HANDOFF_FUNCTIONS:
        raise KeyError(f"Handoff operation is not allowed: {operation}")
    return _invoke_callable(getattr(module, operation), args, kwargs)


def invoke_reflection(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    import application.reflection_service as module

    if operation not in REFLECTION_FUNCTIONS:
        raise KeyError(f"Reflection operation is not allowed: {operation}")
    return _invoke_callable(getattr(module, operation), args, kwargs)
