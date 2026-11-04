from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

from core.config.secrets import resolve_secret


def _local_config_values() -> dict[str, Any]:
    try:
        from local_config import load_local_config
        value = load_local_config()
        return dict(value) if isinstance(value, dict) else {}
    except Exception:
        return {}


def _pick(env: Mapping[str, str], local: Mapping[str, Any], env_key: str, local_key: str, default: Any = "") -> Any:
    value = env.get(env_key)
    if value not in (None, ""):
        return value
    value = local.get(local_key)
    return default if value in (None, "") else value


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str
    username: str
    password: str
    database: str = "neo4j"
    graph_id: str = "financial_graph"
    max_connection_pool_size: int = 20
    connection_timeout_seconds: float = 10.0
    encrypted: bool | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        local_config: Mapping[str, Any] | None = None,
    ) -> "Neo4jSettings":
        source = env or os.environ
        local = dict(local_config or _local_config_values())
        uri = str(_pick(source, local, "NEO4J_URI", "neo4j_uri", "bolt://127.0.0.1:7687")).strip()
        username = str(
            source.get("NEO4J_USERNAME")
            or source.get("NEO4J_USER")
            or local.get("neo4j_username")
            or "neo4j"
        ).strip()
        password = resolve_secret(
            "neo4j_password",
            local_value=local.get("neo4j_password"),
            env_names=("NEO4J_PASSWORD",),
            file_env_names=("STOCK_NEO4J_PASSWORD_FILE",),
            prefer_external=True,
            env=source,
        ).value
        database = str(_pick(source, local, "NEO4J_DATABASE", "neo4j_database", "neo4j")).strip()
        graph_id = str(_pick(source, local, "FINANCIAL_GRAPH_ID", "financial_graph_id", "financial_graph")).strip()
        encrypted_raw = str(_pick(source, local, "NEO4J_ENCRYPTED", "neo4j_encrypted", "")).strip().lower()
        encrypted: bool | None
        if encrypted_raw in {"1", "true", "yes", "on"}:
            encrypted = True
        elif encrypted_raw in {"0", "false", "no", "off"}:
            encrypted = False
        else:
            encrypted = None
        try:
            max_pool = max(1, int(_pick(source, local, "NEO4J_MAX_CONNECTION_POOL_SIZE", "neo4j_max_connection_pool_size", 20)))
        except (TypeError, ValueError):
            max_pool = 20
        try:
            timeout = max(1.0, float(_pick(source, local, "NEO4J_CONNECTION_TIMEOUT_SECONDS", "neo4j_connection_timeout_seconds", 10.0)))
        except (TypeError, ValueError):
            timeout = 10.0
        return cls(
            uri=uri,
            username=username,
            password=password,
            database=database,
            graph_id=graph_id,
            max_connection_pool_size=max_pool,
            connection_timeout_seconds=timeout,
            encrypted=encrypted,
        )

    def validate(self) -> None:
        if not self.uri:
            raise RuntimeError("NEO4J_URI is required")
        if not self.username:
            raise RuntimeError("NEO4J_USERNAME is required")
        if not self.password:
            raise RuntimeError("NEO4J_PASSWORD is required")
        if not self.database:
            raise RuntimeError("NEO4J_DATABASE is required")

    def public_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "username": self.username,
            "database": self.database,
            "graph_id": self.graph_id,
            "max_connection_pool_size": self.max_connection_pool_size,
            "connection_timeout_seconds": self.connection_timeout_seconds,
            "encrypted": self.encrypted,
            "configured": bool(self.password),
        }
