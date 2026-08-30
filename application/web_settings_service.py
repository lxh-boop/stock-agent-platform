from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import config
from core.llm.profiles import build_model_profile
from core.llm.runtime_settings import resolve_active_llm_settings
from local_config import load_local_config, save_local_config
from ranking_runtime.settings import ACTIVE_MODEL_BACKEND, ACTIVE_MODEL_VERSION
from scheduler.runtime_scheduler import reload_runtime_scheduler, scheduler_public_status


class WebSettingsApplicationService:
    """Safe browser boundary for local runtime configuration.

    Secret inputs are accepted only on the write request. Public responses expose
    boolean status and non-secret model metadata, never credential values.
    """

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _env_api_credential() -> str:
        for key in (getattr(config, "LLM_API_KEY_ENV", "LLM_API_KEY"), "OPENAI_API_KEY"):
            value = str(os.environ.get(key, "") or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _env_tushare_credential() -> str:
        return str(os.environ.get("TUSHARE_TOKEN", "") or "").strip()

    @staticmethod
    def _validated_endpoint(value: Any, *, required: bool) -> str:
        endpoint = str(value or "").strip().rstrip("/")
        if not endpoint:
            if required:
                raise ValueError("endpoint_required")
            return ""
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("invalid_endpoint")
        return endpoint

    def public_settings(self) -> dict[str, Any]:
        cfg = load_local_config()
        mode = self._text(cfg.get("llm_mode") or "api").lower()
        if mode not in {"api", "local"}:
            mode = "api"

        runtime = resolve_active_llm_settings(local_config=cfg, mode=mode)
        saved_api_credential = bool(self._text(cfg.get("llm_api_key")))
        default_api_credential = bool(self._env_api_credential())
        saved_tushare = bool(self._text(cfg.get("tushare_token")))
        default_tushare = bool(self._env_tushare_credential())

        api_base_url = self._text(cfg.get("llm_api_base_url")) or self._text(
            os.environ.get(getattr(config, "LLM_BASE_URL_ENV", "LLM_BASE_URL"))
        )
        api_model = self._text(cfg.get("llm_api_model")) or self._text(
            os.environ.get(getattr(config, "LLM_MODEL_ENV", "LLM_MODEL"))
        ) or self._text(getattr(config, "DEFAULT_LLM_MODEL", ""))

        return {
            "universe": self._text(getattr(config, "UNIVERSE", "")),
            "model_backend": ACTIVE_MODEL_BACKEND,
            "model_version": ACTIVE_MODEL_VERSION,
            "default_topk": int(getattr(config, "TOPK", 10) or 10),
            "current_user_id": self._text(cfg.get("current_user_id") or "default"),
            "feature_flags": {
                "news": bool(getattr(config, "ENABLE_NEWS_FEATURES", False)),
                "rag": bool(getattr(config, "ENABLE_RAG", False)),
                "llm_explainer": bool(getattr(config, "ENABLE_LLM_EXPLAINER", False)),
            },
            "credentials": {
                "tushare_configured": bool(saved_tushare or default_tushare),
                "llm_configured": bool(runtime.is_configured),
            },
            "llm": {
                "mode": runtime.mode,
                "provider": runtime.provider,
                "model": runtime.model,
                "endpoint_configured": bool(runtime.base_url),
                "profile_id": runtime.profile_id,
            },
            "configuration": {
                "llm_mode": mode,
                "api": {
                    "provider": self._text(cfg.get("llm_api_provider"))
                    or self._text(getattr(config, "DEFAULT_API_LLM_PROVIDER", "openai_compatible")),
                    "base_url": api_base_url,
                    "model": api_model,
                    "configured": bool(saved_api_credential or default_api_credential),
                    "custom_configured": saved_api_credential,
                    "default_available": default_api_credential,
                },
                "local": {
                    "provider": self._text(getattr(config, "DEFAULT_LOCAL_LLM_PROVIDER", "ollama_local")),
                    "base_url": self._text(cfg.get("llm_local_base_url"))
                    or self._text(getattr(config, "DEFAULT_LOCAL_LLM_BASE_URL", "")),
                    "effective_base_url": runtime.base_url if mode == "local" else "",
                    "model": self._text(cfg.get("llm_local_model"))
                    or self._text(getattr(config, "DEFAULT_LOCAL_LLM_MODEL", "")),
                },
                "tushare": {
                    "configured": bool(saved_tushare or default_tushare),
                    "custom_configured": saved_tushare,
                    "default_available": default_tushare,
                },
            },
            "scheduler": scheduler_public_status(),
            "read_only": False,
        }

    def update_settings(
        self,
        *,
        request_id: str,
        idempotency_key: str,
        confirmed: bool,
        llm_mode: str,
        api_provider: str,
        api_base_url: str,
        api_model: str,
        api_credential: str | None,
        clear_api_credential: bool,
        local_base_url: str,
        local_model: str,
        tushare_credential: str | None,
        clear_tushare_credential: bool,
        scheduler_enabled: bool = False,
        scheduler_hour: int = 20,
        scheduler_minute: int = 0,
        scheduler_catch_up: bool = True,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("settings_update_confirmation_required")
        if not self._text(request_id):
            raise ValueError("request_id_required")
        if not self._text(idempotency_key):
            raise ValueError("idempotency_key_required")

        mode = self._text(llm_mode).lower()
        if mode not in {"api", "local"}:
            raise ValueError("invalid_llm_mode")
        if clear_api_credential and self._text(api_credential):
            raise ValueError("api_credential_clear_conflict")
        if clear_tushare_credential and self._text(tushare_credential):
            raise ValueError("tushare_credential_clear_conflict")
        if not 0 <= int(scheduler_hour) <= 23:
            raise ValueError("invalid_scheduler_hour")
        if not 0 <= int(scheduler_minute) <= 59:
            raise ValueError("invalid_scheduler_minute")

        current = load_local_config()
        updated = dict(current)
        updated.update(
            {
                "llm_mode": mode,
                "llm_api_provider": self._text(api_provider) or "openai_compatible",
                "llm_api_base_url": self._validated_endpoint(api_base_url, required=False),
                "llm_api_model": self._text(api_model),
                "llm_local_base_url": self._validated_endpoint(local_base_url, required=True),
                "llm_local_model": self._text(local_model),
                "auto_retrain_enabled": bool(scheduler_enabled),
                "auto_retrain_hour": int(scheduler_hour),
                "auto_retrain_minute": int(scheduler_minute),
                "auto_retrain_timezone": "Asia/Shanghai",
                "auto_retrain_catch_up": bool(scheduler_catch_up),
            }
        )

        if clear_api_credential:
            updated["llm_api_key"] = ""
        elif api_credential is not None and self._text(api_credential):
            updated["llm_api_key"] = self._text(api_credential)

        if clear_tushare_credential:
            updated["tushare_token"] = ""
        elif tushare_credential is not None and self._text(tushare_credential):
            updated["tushare_token"] = self._text(tushare_credential)

        # Validate both selectable profiles before writing. This rejects URLs
        # containing embedded credentials and invalid local model identifiers.
        build_model_profile(updated, mode="api", credential_ref="configured")
        local_profile = build_model_profile(updated, mode="local", credential_ref="none")
        if local_profile.endpoint_scope != "loopback":
            raise ValueError("local_llm_endpoint_must_be_local")
        if mode == "api" and not self._text(updated.get("llm_api_model")):
            raise ValueError("api_model_required")
        if mode == "local" and not self._text(updated.get("llm_local_model")):
            raise ValueError("local_model_required")

        save_local_config(updated)
        warnings: list[str] = []
        try:
            reload_runtime_scheduler()
        except Exception as exc:
            # 配置已经安全落盘；调度器会在 API 下次启动时重新读取。
            warnings.append(f"scheduler_reload_failed:{type(exc).__name__}")
        return {
            "request_id": self._text(request_id),
            "idempotency_key": self._text(idempotency_key),
            "status": "saved",
            "warnings": warnings,
            "settings": self.public_settings(),
        }


web_settings_service = WebSettingsApplicationService()
