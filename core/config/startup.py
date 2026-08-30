"""Startup validation and readiness reporting.

Liveness answers whether the API process is running. Readiness answers whether
core serving dependencies are usable, while capability readiness (Agent/market)
is reported separately so optional degradation does not make the whole API dead.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from core.config.service_settings import get_service_settings


@dataclass(frozen=True, slots=True)
class StartupCheck:
    name: str
    status: str
    critical: bool
    details: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class StartupReport:
    schema_version: str
    status: str
    core_ready: bool
    agent_ready: bool
    market_data_ready: bool
    config_hash: str
    environment: str
    deployment_mode: str
    checks: tuple[StartupCheck, ...]

    @property
    def ready(self) -> bool:
        return self.core_ready

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "core_ready": self.core_ready,
            "agent_ready": self.agent_ready,
            "market_data_ready": self.market_data_ready,
            "config_hash": self.config_hash,
            "environment": self.environment,
            "deployment_mode": self.deployment_mode,
            "checks": [check.public_dict() for check in self.checks],
        }


def _safe_error_code(prefix: str, exc: BaseException) -> str:
    return f"{prefix}:{type(exc).__name__}"


def _run_check(
    name: str,
    *,
    critical: bool,
    callback: Callable[[], dict[str, Any] | None],
) -> StartupCheck:
    try:
        details = callback() or {}
        return StartupCheck(name=name, status="ok", critical=critical, details=details)
    except Exception as exc:
        return StartupCheck(
            name=name,
            status="failed",
            critical=critical,
            details={},
            error_code=_safe_error_code(name, exc),
        )


def _check_service_config() -> dict[str, Any]:
    settings = get_service_settings()
    # Public readiness output must not expose bind addresses or internal topology.
    return {
        "environment": settings.environment,
        "deployment_mode": settings.deployment_mode,
        "cors_origin_count": len(settings.cors_origins),
        "config_hash": settings.config_hash,
    }


def _check_secret_storage() -> dict[str, Any]:
    from local_config import local_secret_storage_status

    status = local_secret_storage_status()
    if status.get("legacy_plaintext_present"):
        raise RuntimeError("legacy_plaintext_secret_present")
    # Do not publish secret file paths or values.
    return {
        "legacy_plaintext_present": False,
        "configured": dict(status.get("configured") or {}),
    }


def _check_postgres(*, deep: bool) -> dict[str, Any]:
    from database.postgres_config import PostgresSettings

    settings = PostgresSettings.from_env()
    details: dict[str, Any] = {
        "credential_configured": bool(settings.password),
    }
    if deep:
        from database.connection import verify_database

        # Verify real connections but intentionally discard database/user/schema
        # identity returned by the low-level checker. Readiness is a public
        # operational signal, not an infrastructure-discovery endpoint.
        verify_database(runtime=False)
        verify_database(runtime=True)
        details["application_connectivity"] = "ok"
        details["runtime_connectivity"] = "ok"
    return details


def _check_llm() -> dict[str, Any]:
    from core.llm.runtime_settings import resolve_active_llm_settings

    runtime = resolve_active_llm_settings()
    if not runtime.is_configured:
        raise RuntimeError("llm_not_configured")
    return {
        "mode": runtime.mode,
        "provider": runtime.provider,
        "model": runtime.model,
        "profile_id": runtime.profile_id,
        "config_hash": runtime.config_hash,
        "endpoint_scope": runtime.endpoint_scope,
    }


def _check_neo4j(*, deep: bool) -> dict[str, Any]:
    from agent.graph.settings import Neo4jSettings

    settings = Neo4jSettings.from_env()
    settings.validate()
    # Do not publish Neo4j URI, username or database identity in readiness.
    details: dict[str, Any] = {
        "credential_configured": bool(settings.password),
        "encrypted": settings.encrypted,
    }
    if deep:
        from agent.graph.store import Neo4jFinancialGraphStore

        store = Neo4jFinancialGraphStore(settings)
        try:
            store.verify_connectivity()
        finally:
            store.close()
        details["connectivity"] = "ok"
    return details


def _check_tushare() -> dict[str, Any]:
    from core.config.secrets import resolve_secret
    from local_config import load_local_config

    local = load_local_config()
    resolved = resolve_secret(
        "tushare_token",
        local_value=local.get("tushare_token"),
        env_names=("TUSHARE_TOKEN",),
        file_env_names=("STOCK_TUSHARE_TOKEN_FILE",),
        prefer_external=False,
    )
    return resolved.public_dict()


def build_startup_report(*, deep: bool = False) -> StartupReport:
    settings = get_service_settings()
    checks = (
        _run_check("service_config", critical=True, callback=_check_service_config),
        _run_check("secret_storage", critical=True, callback=_check_secret_storage),
        _run_check("postgres", critical=True, callback=lambda: _check_postgres(deep=deep)),
        _run_check("llm", critical=False, callback=_check_llm),
        _run_check("neo4j", critical=False, callback=lambda: _check_neo4j(deep=deep)),
        _run_check("tushare", critical=False, callback=_check_tushare),
    )
    core_ready = all(check.ok for check in checks if check.critical)
    by_name = {check.name: check for check in checks}
    agent_ready = bool(by_name["llm"].ok and by_name["neo4j"].ok and core_ready)
    market_data_ready = bool(by_name["tushare"].ok and core_ready)
    return StartupReport(
        schema_version="startup-readiness.v1",
        status="ready" if core_ready else "not_ready",
        core_ready=core_ready,
        agent_ready=agent_ready,
        market_data_ready=market_data_ready,
        config_hash=settings.config_hash,
        environment=settings.environment,
        deployment_mode=settings.deployment_mode,
        checks=checks,
    )


_CACHE_LOCK = threading.Lock()
_CACHE: tuple[float, bool, StartupReport] | None = None


def get_startup_report(*, deep: bool = True, ttl_seconds: float | None = None) -> StartupReport:
    """Return a small cached readiness snapshot to avoid probe connection storms."""

    ttl = float(
        ttl_seconds
        if ttl_seconds is not None
        else os.environ.get("STOCK_APP_READINESS_CACHE_SECONDS", "5")
    )
    ttl = max(0.0, ttl)
    now = time.monotonic()
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            created_at, cached_deep, report = _CACHE
            if cached_deep == bool(deep) and now - created_at <= ttl:
                return report
        report = build_startup_report(deep=deep)
        _CACHE = (now, bool(deep), report)
        return report


def invalidate_startup_cache() -> None:
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


__all__ = [
    "StartupCheck",
    "StartupReport",
    "build_startup_report",
    "get_startup_report",
    "invalidate_startup_cache",
]
