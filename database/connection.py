from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from database.postgres_config import PostgresSettings


def _driver():
    try:
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("psycopg_not_installed") from exc
    return psycopg, dict_row


def get_connection(*, runtime: bool = False, scoped: bool = True):
    """Return a PostgreSQL connection with the application schema on search_path."""
    settings = PostgresSettings.from_env()
    psycopg, dict_row = _driver()
    schema = settings.runtime_schema if runtime else settings.app_schema
    conn = psycopg.connect(**settings.connection_kwargs(), row_factory=dict_row)
    try:
        from core.user_scope import current_owner
        owner = current_owner() if scoped and not runtime else ""
        if owner:
            from database.user_data_schema import ensure_user_data_schema
            data_schema = ensure_user_data_schema(conn, settings, owner)
            # 所有 DATA_TABLES 必须存在；初始化失败即报错，不能回落公共数据。
            conn.execute(f'SET search_path TO "{data_schema}", "{schema}", public')
        else:
            conn.execute(f'SET search_path TO "{schema}", public')
        return conn
    except Exception:
        conn.close()
        raise


@contextmanager
def transaction(*, runtime: bool = False, scoped: bool = True) -> Iterator[object]:
    conn = get_connection(runtime=runtime, scoped=scoped)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_database(*, runtime: bool = False) -> dict[str, str]:
    settings = PostgresSettings.from_env()
    schema = settings.runtime_schema if runtime else settings.app_schema
    with transaction(runtime=runtime) as conn:
        row = conn.execute(
            "SELECT current_database() AS database, current_user AS username, current_schema() AS schema"
        ).fetchone()
    return {
        "database": str(row.get("database") or settings.database),
        "username": str(row.get("username") or settings.user),
        "schema": str(row.get("schema") or schema),
    }
