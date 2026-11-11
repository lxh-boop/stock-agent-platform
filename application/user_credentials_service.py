"""Account-scoped credential resolution.

No fallback to other accounts' credentials or platform-wide API keys.
Platform scheduled market updates continue to use system credentials.
"""
from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from core.config.user_credentials import decrypt_credential, encrypt_credential
from database.repositories.auth_repository import AuthRepository

LLM_CONFIG_KEYS = (
    "llm_mode", "llm_api_provider", "llm_api_base_url", "llm_api_model",
    "llm_local_base_url", "llm_local_model",
)


def public_user_settings(user_id: str) -> dict[str, Any]:
    row = AuthRepository().account_settings(user_id)
    config = row["config"]
    return {
        "config": {key: config[key] for key in LLM_CONFIG_KEYS if key in config},
        "llm_configured": bool(row["llm_api_key_ciphertext"]),
        "tushare_configured": bool(row["tushare_token_ciphertext"]),
    }


def update_user_settings(
    user_id: str, *, config: dict[str, Any],
    llm_key: str | None = None, tushare_token: str | None = None,
    clear_llm: bool = False, clear_tushare: bool = False,
) -> None:
    repository = AuthRepository()
    current = repository.account_settings(user_id)
    merged = dict(current["config"])
    merged.update({key: config[key] for key in LLM_CONFIG_KEYS if key in config})
    if clear_llm and llm_key:
        raise ValueError("api_credential_clear_conflict")
    if clear_tushare and tushare_token:
        raise ValueError("tushare_credential_clear_conflict")
    encrypted_llm = current["llm_api_key_ciphertext"]
    encrypted_tushare = current["tushare_token_ciphertext"]
    if clear_llm:
        encrypted_llm = ""
    elif llm_key:
        encrypted_llm = encrypt_credential(llm_key)
    if clear_tushare:
        encrypted_tushare = ""
    elif tushare_token:
        encrypted_tushare = encrypt_credential(tushare_token)
    repository.save_account_settings(
        user_id, config=merged,
        llm_ciphertext=encrypted_llm,
        tushare_ciphertext=encrypted_tushare,
    )


def get_user_llm_settings(user_id: str):
    """Resolve a strict per-user snapshot for a LangGraph Agent run."""
    from core.llm.profiles import build_model_profile
    from core.llm.runtime_settings import LLMRuntimeSettings

    data = AuthRepository().account_settings(user_id)
    config = data["config"]
    selected_mode = str(config.get("llm_mode") or "api")
    if selected_mode not in {"api", "local"}:
        raise ValueError("invalid_llm_mode")
    if selected_mode == "api":
        credential = decrypt_credential(data["llm_api_key_ciphertext"])
        if not credential:
            raise ValueError("user_llm_credential_not_configured")
    else:
        credential = ""
    profile = build_model_profile(
        config, mode=selected_mode,
        credential_ref="account" if selected_mode == "api" else "none",
    )
    return LLMRuntimeSettings(profile=profile, credential=credential)


def get_user_tushare_token(user_id: str) -> str:
    data = AuthRepository().account_settings(user_id)
    token = decrypt_credential(data["tushare_token_ciphertext"])
    if not token:
        raise ValueError("user_tushare_token_not_configured")
    return token

def public_settings_for_user(user_id: str) -> dict[str, Any]:
    """Combine shared read-only runtime status with account-owned editable fields."""
    import config as defaults
    from core.llm.profiles import build_model_profile
    from ranking_runtime.settings import ACTIVE_MODEL_BACKEND, ACTIVE_MODEL_VERSION
    from scheduler.account_scheduler import public_status

    # 页面只读取密文是否存在，不解密 Token，也不读取别人的/平台的设置。
    shared = {
        "universe": defaults.UNIVERSE, "model_backend": ACTIVE_MODEL_BACKEND,
        "model_version": ACTIVE_MODEL_VERSION, "default_topk": int(getattr(defaults, "TOPK", 10)),
        "feature_flags": {"news": defaults.ENABLE_NEWS_FEATURES, "rag": defaults.ENABLE_RAG,
                          "llm_explainer": defaults.ENABLE_LLM_EXPLAINER},
        "scheduler": public_status(user_id), "read_only": False,
    }
    account = public_user_settings(user_id)
    cfg = account["config"]
    mode = str(cfg.get("llm_mode") or "api")
    profile = build_model_profile(cfg, mode=mode, credential_ref="account")
    shared["current_user_id"] = user_id
    shared["credentials"] = {
        "tushare_configured": account["tushare_configured"],
        "llm_configured": (account["llm_configured"] if mode == "api" else bool(profile.model_name)),
    }
    shared["llm"] = {
        "mode": mode, "provider": profile.provider_id,
        "model": profile.model_name,
        "endpoint_configured": bool(profile.base_url),
        "profile_id": profile.profile_id,
    }
    shared["configuration"] = {
        "llm_mode": mode,
        "api": {
            "provider": cfg.get("llm_api_provider") or "openai_compatible",
            "base_url": cfg.get("llm_api_base_url") or "",
            "model": cfg.get("llm_api_model") or "",
            "configured": account["llm_configured"],
            "custom_configured": account["llm_configured"],
            "default_available": False,
        },
        "local": {
            "provider": "ollama_local",
            "base_url": cfg.get("llm_local_base_url") or "http://127.0.0.1:11434/v1",
            "effective_base_url": profile.base_url if mode == "local" else "",
            "model": cfg.get("llm_local_model") or "stock-agent-qwen3-4b",
        },
        "tushare": {
            "configured": account["tushare_configured"],
            "custom_configured": account["tushare_configured"],
            "default_available": False,
        },
    }
    return shared


def _validate_user_api_endpoint(url: str) -> None:
    """Prevent user-editable API URLs from targeting local infrastructure."""
    if not url:
        return
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise ValueError("user_api_endpoint_must_be_public_https")
    if hostname in {"localhost", "host.docker.internal"} or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise ValueError("user_api_endpoint_must_be_public_https")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("user_api_endpoint_must_be_public_https")


def save_settings_for_user(user_id: str, *, is_admin: bool, values: dict[str, Any]) -> dict[str, Any]:
    """All credential changes are owned by the authenticated user."""
    from core.llm.profiles import build_model_profile

    if not values.get("confirmed"):
        raise ValueError("settings_update_confirmation_required")
    mode = str(values.get("llm_mode") or "api")
    config = {
        "llm_mode": mode,
        "llm_api_provider": str(values.get("api_provider") or "openai_compatible"),
        "llm_api_base_url": str(values.get("api_base_url") or "").strip().rstrip("/"),
        "llm_api_model": str(values.get("api_model") or "").strip(),
        "llm_local_base_url": str(values.get("local_base_url") or "").strip(),
        "llm_local_model": str(values.get("local_model") or "").strip(),
    }
    if mode == "api" and not config["llm_api_model"]:
        raise ValueError("api_model_required")
    if mode == "local" and not config["llm_local_model"]:
        raise ValueError("local_model_required")
    _validate_user_api_endpoint(config["llm_api_base_url"])
    profile = build_model_profile(config, mode=mode, credential_ref="account")
    if mode == "local" and profile.endpoint_scope != "loopback":
        raise ValueError("local_llm_endpoint_must_be_local")
    from scheduler.account_scheduler import normalize_schedule
    schedule = normalize_schedule({
        "auto_retrain_enabled": values.get("scheduler_enabled", False),
        "auto_retrain_hour": values.get("scheduler_hour", 20),
        "auto_retrain_minute": values.get("scheduler_minute", 0),
        "auto_retrain_catch_up": values.get("scheduler_catch_up", True),
    })
    update_user_settings(
        user_id, config=config,
        llm_key=values.get("api_credential"),
        tushare_token=values.get("tushare_credential"),
        clear_llm=bool(values.get("clear_api_credential")),
        clear_tushare=bool(values.get("clear_tushare_credential")),
    )
    AuthRepository().save_scheduler_settings(user_id, schedule)
    return {
        "request_id": str(values.get("request_id") or ""),
        "idempotency_key": str(values.get("idempotency_key") or ""),
        "status": "saved",
        "settings": public_settings_for_user(user_id),
    }


def private_credential_for_user(user_id: str, secret_id: str) -> str:
    columns = {"llm_api_key": "llm_api_key_ciphertext", "tushare_token": "tushare_token_ciphertext"}
    if secret_id not in columns:
        raise ValueError("unsupported_account_credential")
    return decrypt_credential(AuthRepository().account_settings(user_id)[columns[secret_id]])


def runtime_config_for_user(user_id: str) -> dict[str, Any]:
    from local_config import DEFAULT_LOCAL_CONFIG
    repo = AuthRepository()
    account = repo.account_settings(user_id)
    # 拷贝静态默认值，而非读取 local_app_config.json 或服务端个人 Token 文件。
    cfg = dict(DEFAULT_LOCAL_CONFIG)
    cfg.update({key: value for key, value in account["config"].items() if key in LLM_CONFIG_KEYS})
    cfg["current_user_id"] = user_id
    cfg["llm_api_key"] = decrypt_credential(account["llm_api_key_ciphertext"])
    cfg["tushare_token"] = decrypt_credential(account["tushare_token_ciphertext"])
    cfg.update(repo.scheduler_settings(user_id))
    return cfg
