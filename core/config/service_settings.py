"""Typed service/runtime configuration for the enterprise API boundary."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Mapping


def _text(value: object) -> str:
    return str(value or "").strip()


def _bool(value: object, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return _text(value).lower() in {"1", "true", "yes", "on", "enabled"}


def _int(value: object, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _cors(value: object) -> tuple[str, ...]:
    return tuple(item.strip() for item in _text(value).split(",") if item.strip())


def _port(value: object, default: int = 8010) -> int:
    text = _text(value)
    if not text:
        return int(default)
    try:
        parsed = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_api_port") from exc
    if not 1 <= parsed <= 65535:
        raise ValueError("invalid_api_port")
    return parsed


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    schema_version: str
    environment: str
    deployment_mode: str
    api_host: str
    api_port: int
    api_reload: bool
    cors_origins: tuple[str, ...]
    recover_interrupted_on_start: bool
    runtime_scheduler_enabled: bool
    max_concurrent_tasks: int
    max_parallel_requests: int
    max_parallel_workers: int
    max_parallel_tools: int
    max_parallel_llm: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServiceSettings":
        source = env or os.environ
        return cls(
            schema_version="service-settings.v1",
            environment=_text(source.get("STOCK_APP_ENV")) or "local",
            deployment_mode=_text(source.get("STOCK_APP_DEPLOYMENT_MODE")) or "local",
            api_host=_text(source.get("AGENT_API_HOST")) or "127.0.0.1",
            api_port=_port(source.get("AGENT_API_PORT"), 8010),
            api_reload=_bool(source.get("AGENT_API_RELOAD"), False),
            cors_origins=_cors(
                source.get(
                    "STOCK_AGENT_CORS_ORIGINS",
                    "http://localhost:3000,http://127.0.0.1:3000",
                )
            ),
            recover_interrupted_on_start=_bool(
                source.get("STOCK_AGENT_RECOVER_INTERRUPTED_ON_START"), False
            ),
            runtime_scheduler_enabled=_bool(
                source.get("STOCK_APP_RUNTIME_SCHEDULER_ENABLED"), True
            ),
            max_concurrent_tasks=_int(source.get("STOCK_AGENT_MAX_CONCURRENT_TASKS"), 4),
            max_parallel_requests=_int(source.get("AGENT_MAX_PARALLEL_REQUESTS"), 3),
            max_parallel_workers=_int(source.get("AGENT_MAX_PARALLEL_WORKERS"), 6),
            max_parallel_tools=_int(source.get("AGENT_MAX_PARALLEL_TOOLS"), 8),
            max_parallel_llm=_int(source.get("AGENT_MAX_PARALLEL_LLM"), 4),
        )

    def validate(self) -> None:
        if not self.api_host:
            raise ValueError("api_host_required")
        if not 1 <= int(self.api_port) <= 65535:
            raise ValueError("invalid_api_port")
        if not self.cors_origins:
            raise ValueError("cors_origins_required")
        if self.environment.lower() in {"staging", "production"} and "*" in self.cors_origins:
            raise ValueError("wildcard_cors_forbidden_in_managed_environment")
        for name, value in (
            ("max_concurrent_tasks", self.max_concurrent_tasks),
            ("max_parallel_requests", self.max_parallel_requests),
            ("max_parallel_workers", self.max_parallel_workers),
            ("max_parallel_tools", self.max_parallel_tools),
            ("max_parallel_llm", self.max_parallel_llm),
        ):
            if int(value) < 1:
                raise ValueError(f"invalid_{name}")

    @property
    def public_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        encoded = json.dumps(
            self.public_dict,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def get_service_settings(env: Mapping[str, str] | None = None) -> ServiceSettings:
    settings = ServiceSettings.from_env(env)
    settings.validate()
    return settings


__all__ = ["ServiceSettings", "get_service_settings"]
