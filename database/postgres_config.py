from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re

_VALID_SCHEMA = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_secret(path_value: str) -> str:
    path = Path(str(path_value or "").strip())
    if not path_value or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class PostgresSettings:
    host: str = "postgres"
    port: int = 5432
    database: str = "stock_daily_app"
    user: str = "stock_app"
    password: str = ""
    app_schema: str = "stock_app"
    runtime_schema: str = "stock_runtime"
    connect_timeout: int = 8

    @classmethod
    def from_env(cls) -> "PostgresSettings":
        app_schema = str(os.getenv("STOCK_POSTGRES_SCHEMA", "stock_app")).strip() or "stock_app"
        runtime_schema = str(os.getenv("STOCK_POSTGRES_RUNTIME_SCHEMA", "stock_runtime")).strip() or "stock_runtime"
        for schema in (app_schema, runtime_schema):
            if not _VALID_SCHEMA.fullmatch(schema):
                raise ValueError(f"invalid_postgres_schema:{schema}")
        password = str(os.getenv("STOCK_POSTGRES_PASSWORD", "")).strip()
        if not password:
            default_secret = r"D:\google\postgres_final\postgres_password.txt" if os.name == "nt" else ""
            password = _read_secret(os.getenv("STOCK_POSTGRES_PASSWORD_FILE", default_secret))
        if not password:
            raise RuntimeError("postgres_credentials_required")
        return cls(
            host=str(os.getenv("STOCK_POSTGRES_HOST", "127.0.0.1" if os.name == "nt" else "postgres")).strip() or ("127.0.0.1" if os.name == "nt" else "postgres"),
            port=int(os.getenv("STOCK_POSTGRES_PORT", "5432")),
            database=str(os.getenv("STOCK_POSTGRES_DB", "stock_daily_app")).strip() or "stock_daily_app",
            user=str(os.getenv("STOCK_POSTGRES_USER", "stock_app")).strip() or "stock_app",
            password=password,
            app_schema=app_schema,
            runtime_schema=runtime_schema,
            connect_timeout=max(1, int(os.getenv("STOCK_POSTGRES_CONNECT_TIMEOUT", "8"))),
        )

    def connection_kwargs(self) -> dict[str, object]:
        return {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "password": self.password,
            "connect_timeout": self.connect_timeout,
        }
