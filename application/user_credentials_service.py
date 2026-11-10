"""Account-scoped credential resolution.

No fallback to other accounts' credentials or platform-wide API keys.
Platform scheduled market updates continue to use system credentials.
"""
from __future__ import annotations

from typing import Any

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
