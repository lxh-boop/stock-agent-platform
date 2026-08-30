import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict

from core.config.paths import get_local_config_path, is_frozen_app
from core.config.secrets import LocalSecretStore, SECRET_IDS


LOCAL_CONFIG_PATH = (
    str(get_local_config_path()) if is_frozen_app() else "local_app_config.json"
)


_CONFIG_WRITE_LOCK = threading.Lock()


DEFAULT_LOCAL_CONFIG = {
    # Secret keys stay in the in-memory compatibility schema, but persisted JSON
    # always stores them blank. Values live in LocalSecretStore instead.
    "tushare_token": "",
    "llm_api_key": "",
    "llm_mode": "api",
    "llm_api_provider": "openai_compatible",
    "llm_api_base_url": "",
    "llm_api_model": "",
    "llm_api_disable_thinking": False,
    "llm_api_context_window": 128000,
    "llm_api_supports_json_schema": True,
    "llm_api_supports_tools": True,
    "llm_local_base_url": "http://127.0.0.1:11434/v1",
    "llm_local_model": "stock-agent-qwen3-4b",
    "llm_local_disable_thinking": True,
    "llm_local_context_window": 32768,
    "llm_local_supports_json_schema": False,
    "llm_local_supports_tools": False,
    "llm_request_timeout_seconds": 99120,
    "llm_max_retries": 0,
    "current_user_id": "default",
    "model_backend": "registered_ranker",
    "auto_retrain_enabled": False,
    "auto_retrain_hour": 20,
    "auto_retrain_minute": 0,
    "auto_retrain_timezone": "Asia/Shanghai",
    "auto_retrain_catch_up": True,
    "scheduler_market_update_timeout_seconds": 997200,
    "model_version": "latest",
    "page_zoom_percent": 100,
    "mcp_data_enabled": True,
    "mcp_data_allowed_tools": [
        "get_user_profile",
        "get_portfolio_state",
        "get_positions",
        "get_orders",
        "get_stock_info",
        "get_latest_ranking",
        "get_latest_recommendations",
    ],
    "mcp_data_timeout_seconds": 30.0,
    "mcp_rag_enabled": True,
    "mcp_rag_allowed_tools": [
        "search_documents",
        "search_news",
        "retrieve_evidence",
    ],
    "mcp_rag_timeout_seconds": 90.0,
    "mcp_model_enabled": True,
    "mcp_model_allowed_tools": [
        "predict_stock_score",
        "predict_rank",
        "predict_risk",
    ],
    "mcp_model_timeout_seconds": 30.0,
    "mcp_external_servers": [],
    "mcp_discovery_ttl_seconds": 300,
    "neo4j_uri": "bolt://127.0.0.1:7687",
    "neo4j_username": "neo4j",
    "neo4j_password": "",
    "neo4j_database": "neo4j",
    "financial_graph_id": "financial_graph",
    "neo4j_encrypted": "",
    "neo4j_connection_timeout_seconds": 10.0,
    "neo4j_max_connection_pool_size": 20,
}


def _legacy_compatible_view(config: Dict[str, Any]) -> Dict[str, Any]:
    """Expose read-only aliases for explicitly unmigrated legacy consumers."""

    view = dict(config)
    view["llm_base_url"] = str(view.get("llm_api_base_url") or "")
    view["llm_model"] = str(view.get("llm_api_model") or "")
    return view


def _merge_local_secret_store(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(config)
    store = LocalSecretStore()
    for key in SECRET_IDS:
        if str(merged.get(key) or "").strip():
            continue
        value = store.read(key)
        if value:
            merged[key] = value
    return merged


def _normalise_config(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = DEFAULT_LOCAL_CONFIG.copy()
    cfg.update(config)
    for obsolete_key in (
        "mcp_example_enabled",
        "mcp_example_allowed_tools",
        "mcp_example_timeout_seconds",
    ):
        cfg.pop(obsolete_key, None)
    if not str(cfg.get("llm_api_base_url") or "").strip() and str(cfg.get("llm_base_url") or "").strip():
        cfg["llm_api_base_url"] = str(cfg["llm_base_url"]).strip()
    if not str(cfg.get("llm_api_model") or "").strip() and str(cfg.get("llm_model") or "").strip():
        cfg["llm_api_model"] = str(cfg["llm_model"]).strip()
    if str(cfg.get("llm_mode") or "").strip().lower() not in {"api", "local"}:
        cfg["llm_mode"] = "api"
    cfg.pop("llm_base_url", None)
    cfg.pop("llm_model", None)
    return cfg


def _write_payload(path: Path, cfg: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        except OSError:
            # A single-file Docker bind mount cannot be replaced, but it can
            # be updated. Copy only after the complete JSON was fsynced.
            with temporary.open("r", encoding="utf-8") as source, path.open(
                "w", encoding="utf-8", newline="\n"
            ) as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
    finally:
        temporary.unlink(missing_ok=True)


def load_local_config() -> Dict[str, Any]:
    path = Path(LOCAL_CONFIG_PATH)
    if not path.exists():
        return _legacy_compatible_view(_merge_local_secret_store(DEFAULT_LOCAL_CONFIG))

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        cfg = _normalise_config(dict(data) if isinstance(data, dict) else {})
        # Compatibility: until startup migration runs, legacy plaintext values
        # remain readable. Once migrated, LocalSecretStore overlays them.
        cfg = _merge_local_secret_store(cfg)
        return _legacy_compatible_view(cfg)

    except Exception:
        return _legacy_compatible_view(_merge_local_secret_store(DEFAULT_LOCAL_CONFIG))


def save_local_config(config: Dict[str, Any]) -> None:
    """Persist one validated non-secret configuration snapshot.

    Secret fields are accepted for legacy/UI compatibility, but are written to
    ``LocalSecretStore`` outside the source tree. The JSON file always contains
    blank secret fields so Git/build artifacts cannot capture credentials.
    """

    input_config = dict(config or {})
    cfg = _normalise_config(input_config)
    store = LocalSecretStore()

    # Only mutate a secret when the caller explicitly supplied the key. Partial
    # non-secret saves must never clear an existing credential.
    for secret_id in SECRET_IDS:
        if secret_id in input_config:
            value = str(input_config.get(secret_id) or "").strip()
            if value:
                store.write(secret_id, value)
            else:
                store.clear(secret_id)
        cfg[secret_id] = ""

    path = Path(LOCAL_CONFIG_PATH)
    with _CONFIG_WRITE_LOCK:
        _write_payload(path, cfg)


def migrate_legacy_local_config_secrets() -> dict[str, Any]:
    """Move plaintext secrets from legacy ``local_app_config.json`` safely.

    Migration is atomic from the application's point of view: all secret values
    are written to the local secret store before the JSON is sanitized. If a
    secret-store write raises, the source JSON is left untouched.
    """

    path = Path(LOCAL_CONFIG_PATH)
    if not path.exists():
        return {"schema_version": "secret_migration.v1", "migrated": [], "changed": False}

    with _CONFIG_WRITE_LOCK:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("local_config_not_object")

        candidates = {
            secret_id: str(raw.get(secret_id) or "").strip()
            for secret_id in SECRET_IDS
            if str(raw.get(secret_id) or "").strip()
        }
        if not candidates:
            return {"schema_version": "secret_migration.v1", "migrated": [], "changed": False}

        store = LocalSecretStore()
        for secret_id, value in candidates.items():
            store.write(secret_id, value)

        sanitized = dict(raw)
        for secret_id in SECRET_IDS:
            sanitized[secret_id] = ""
        _write_payload(path, _normalise_config(sanitized))
        return {
            "schema_version": "secret_migration.v1",
            "migrated": sorted(candidates),
            "changed": True,
        }


def local_secret_storage_status() -> dict[str, Any]:
    store = LocalSecretStore()
    path = Path(LOCAL_CONFIG_PATH)
    legacy_plaintext_keys: list[str] = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                legacy_plaintext_keys = [
                    key for key in SECRET_IDS if str(raw.get(key) or "").strip()
                ]
        except Exception:
            legacy_plaintext_keys = ["<unreadable_local_config>"]
    return {
        "schema_version": "secret_storage_status.v1",
        "legacy_plaintext_present": bool(legacy_plaintext_keys),
        "legacy_plaintext_keys": legacy_plaintext_keys,
        "configured": {key: bool(store.read(key)) for key in SECRET_IDS},
    }
