"""Resolve one credential-bearing runtime snapshot per Agent run."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from config import DEFAULT_LLM_MODE, LLM_API_KEY_ENV, LLM_BASE_URL_ENV, LLM_MODEL_ENV
from core.config.secrets import resolve_secret
from core.llm.profiles import ModelProfile, build_model_profile


def _text(value: Any) -> str:
    return str(value or "").strip()


def migrate_legacy_llm_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Read legacy aliases only at the configuration migration boundary."""

    migrated = dict(config or {})
    migrated.setdefault("llm_mode", DEFAULT_LLM_MODE)
    if not _text(migrated.get("llm_api_base_url")) and _text(migrated.get("llm_base_url")):
        migrated["llm_api_base_url"] = _text(migrated.get("llm_base_url"))
    if not _text(migrated.get("llm_api_model")) and _text(migrated.get("llm_model")):
        migrated["llm_api_model"] = _text(migrated.get("llm_model"))
    return migrated


@dataclass(frozen=True)
class LLMRuntimeSettings:
    profile: ModelProfile
    credential: str = field(default="", repr=False)

    @property
    def profile_id(self) -> str:
        return self.profile.profile_id

    @property
    def mode(self) -> str:
        return self.profile.deployment_mode

    @property
    def provider(self) -> str:
        return self.profile.provider_id

    @property
    def api_key(self) -> str:
        """Legacy compatibility accessor; never included in public output."""

        return "ollama" if self.mode == "local" else self.credential

    @property
    def base_url(self) -> str:
        return self.profile.base_url

    @property
    def model(self) -> str:
        return self.profile.model_name

    @property
    def disable_thinking(self) -> bool:
        return self.profile.disable_thinking

    @property
    def request_timeout_seconds(self) -> int:
        return self.profile.request_timeout_seconds

    @property
    def max_retries(self) -> int:
        return self.profile.max_retries

    @property
    def endpoint_scope(self) -> str:
        return self.profile.endpoint_scope

    @property
    def public_dict(self) -> dict[str, Any]:
        return self.profile.public_dict

    @property
    def config_hash(self) -> str:
        return self.profile.config_hash

    @property
    def is_configured(self) -> bool:
        if self.mode == "local":
            return bool(self.model and self.base_url)
        return bool(self.model and self.credential)

    def to_legacy_kwargs(self) -> dict[str, str]:
        return {"api_key": self.api_key, "base_url": self.base_url, "model": self.model}


def _load_saved_config() -> dict[str, Any]:
    try:
        from local_config import load_local_config

        return migrate_legacy_llm_config(load_local_config())
    except Exception:
        return migrate_legacy_llm_config({})


def _credential(saved: Mapping[str, Any], explicit: str | None) -> tuple[str, str]:
    resolved = resolve_secret(
        "llm_api_key",
        explicit=explicit,
        local_value=saved.get("llm_api_key"),
        env_names=(LLM_API_KEY_ENV, "OPENAI_API_KEY"),
        file_env_names=("STOCK_LLM_API_KEY_FILE",),
        prefer_external=False,
    )
    return resolved.value, resolved.source


def resolve_active_llm_settings(
    *,
    local_config: Mapping[str, Any] | None = None,
    profile_id: str | None = None,
    mode: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> LLMRuntimeSettings:
    """Resolve one profile exactly once; legacy raw args are boundary-only."""

    saved = migrate_legacy_llm_config(local_config) if local_config is not None else _load_saved_config()
    configured = dict(saved)
    if base_url is not None:
        configured["llm_api_base_url"] = _text(base_url)
    elif not _text(configured.get("llm_api_base_url")):
        configured["llm_api_base_url"] = _text(os.environ.get(LLM_BASE_URL_ENV))
    if model is not None:
        configured["llm_api_model"] = _text(model)
    elif not _text(configured.get("llm_api_model")):
        configured["llm_api_model"] = _text(os.environ.get(LLM_MODEL_ENV))

    explicit_mode = _text(mode).lower() if mode is not None else ""
    selected_mode = _text(explicit_mode or configured.get("llm_mode") or DEFAULT_LLM_MODE).lower()
    if selected_mode not in {"api", "local"}:
        selected_mode = DEFAULT_LLM_MODE

    # The selected mode is authoritative. A stale profile identifier must never
    # switch a new request from API back to local Ollama (or vice versa).
    if profile_id:
        requested_profile_id = str(profile_id)
        candidate = build_model_profile(
            configured,
            mode=selected_mode,
            credential_ref="none",
        )
        saved_profile_id = _text(configured.get(f"llm_{selected_mode}_profile_id"))
        allowed_profile_ids = {candidate.profile_id}
        if saved_profile_id:
            allowed_profile_ids.add(saved_profile_id)
        if requested_profile_id not in allowed_profile_ids:
            raise ValueError(
                f"Model Profile mode mismatch: profile {requested_profile_id} "
                f"does not belong to selected mode {selected_mode}"
            )

    credential, credential_ref = _credential(configured, api_key)
    profile = build_model_profile(
        configured,
        mode=selected_mode,
        credential_ref=credential_ref if selected_mode == "api" else "none",
    )
    if profile_id and profile.profile_id != str(profile_id):
        # Explicit saved profile identifiers are stable configuration slots;
        # any mismatch here means the selected profile changed during resolution.
        if _text(configured.get(f"llm_{selected_mode}_profile_id")) != str(profile_id):
            raise ValueError(f"Unknown Model Profile: {profile_id}")
    return LLMRuntimeSettings(
        profile=profile,
        credential="" if profile.deployment_mode == "local" else credential,
    )


__all__ = ["LLMRuntimeSettings", "migrate_legacy_llm_config", "resolve_active_llm_settings"]
