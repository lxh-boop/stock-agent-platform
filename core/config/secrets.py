"""Secret storage and resolution helpers.

Enterprise E1 boundary:
- application config files contain only non-secret configuration;
- local interactive credentials are stored outside the source tree;
- environment/file-managed secrets can override local storage at explicit call sites;
- public status never exposes secret values.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping


SECRET_IDS = ("llm_api_key", "tushare_token", "neo4j_password")
_SECRET_FILENAMES = {
    "llm_api_key": "llm_api_key.secret",
    "tushare_token": "tushare_token.secret",
    "neo4j_password": "neo4j_password.secret",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def get_secret_dir(env: Mapping[str, str] | None = None) -> Path:
    source = env or os.environ
    explicit = _text(source.get("STOCK_APP_SECRET_DIR"))
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        local_app_data = _text(source.get("LOCALAPPDATA"))
        if local_app_data:
            return Path(local_app_data) / "StockDailyApp" / "secrets"
    xdg = _text(source.get("XDG_CONFIG_HOME"))
    if xdg:
        return Path(xdg).expanduser() / "stock-daily-app" / "secrets"
    return Path.home() / ".config" / "stock-daily-app" / "secrets"


def _secret_path(secret_id: str, env: Mapping[str, str] | None = None) -> Path:
    if secret_id not in _SECRET_FILENAMES:
        raise KeyError(f"unknown_secret_id:{secret_id}")
    return get_secret_dir(env) / _SECRET_FILENAMES[secret_id]


def _read_file(path_value: object) -> str:
    path_text = _text(path_value)
    if not path_text:
        return ""
    path = Path(path_text).expanduser()
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class SecretResolution:
    secret_id: str
    value: str = field(default="", repr=False)
    source: str = "unconfigured"
    externally_managed: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.value)

    def public_dict(self) -> dict[str, object]:
        return {
            "secret_id": self.secret_id,
            "configured": self.configured,
            "source": self.source,
            "externally_managed": self.externally_managed,
        }


class LocalSecretStore:
    """Small file-backed local secret store outside the project tree.

    This is the local-development/browser-settings backend. Production can use
    environment variables or *_FILE mounts; those are resolved at the call site
    and are not copied into this store.
    """

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self._env = env or os.environ

    @property
    def root(self) -> Path:
        return get_secret_dir(self._env)

    def read(self, secret_id: str) -> str:
        path = _secret_path(secret_id, self._env)
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def write(self, secret_id: str, value: str) -> None:
        secret = _text(value)
        if not secret:
            self.clear(secret_id)
            return
        path = _secret_path(secret_id, self._env)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(secret)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self, secret_id: str) -> None:
        path = _secret_path(secret_id, self._env)
        path.unlink(missing_ok=True)

    def status(self, secret_id: str) -> dict[str, object]:
        return {
            "secret_id": secret_id,
            "configured": bool(self.read(secret_id)),
            "source": "local_secret_store" if self.read(secret_id) else "unconfigured",
            "externally_managed": False,
        }


def resolve_secret(
    secret_id: str,
    *,
    explicit: str | None = None,
    local_value: object = "",
    env_names: Iterable[str] = (),
    file_env_names: Iterable[str] = (),
    prefer_external: bool = False,
    env: Mapping[str, str] | None = None,
) -> SecretResolution:
    """Resolve a secret without ever serializing its value.

    ``prefer_external=False`` preserves the application's historical local
    settings precedence (used by LLM/Tushare). ``prefer_external=True`` is used
    for infrastructure credentials such as Neo4j where environment management
    is authoritative.
    """

    source = env or os.environ
    if explicit is not None and _text(explicit):
        return SecretResolution(secret_id, _text(explicit), "runtime:explicit", False)

    def external_candidates() -> list[SecretResolution]:
        rows: list[SecretResolution] = []
        for name in env_names:
            value = _text(source.get(name))
            if value:
                rows.append(SecretResolution(secret_id, value, f"env:{name}", True))
        for name in file_env_names:
            path_value = _text(source.get(name))
            value = _read_file(path_value)
            if value:
                rows.append(SecretResolution(secret_id, value, f"file_env:{name}", True))
        return rows

    external = external_candidates()
    local_candidates = []
    if _text(local_value):
        local_candidates.append(
            SecretResolution(secret_id, _text(local_value), "local_config_or_secret_store", False)
        )

    ordered = [*external, *local_candidates] if prefer_external else [*local_candidates, *external]
    return ordered[0] if ordered else SecretResolution(secret_id)


def public_secret_status(
    secret_id: str,
    *,
    local_value: object = "",
    env_names: Iterable[str] = (),
    file_env_names: Iterable[str] = (),
    prefer_external: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    return resolve_secret(
        secret_id,
        local_value=local_value,
        env_names=env_names,
        file_env_names=file_env_names,
        prefer_external=prefer_external,
        env=env,
    ).public_dict()


__all__ = [
    "LocalSecretStore",
    "SECRET_IDS",
    "SecretResolution",
    "get_secret_dir",
    "public_secret_status",
    "resolve_secret",
]
