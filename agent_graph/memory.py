"""LangGraph PostgreSQL persistence assembly.

Provides the checkpointer (interrupt/approval state + conversation threads)
and the long-term store using the project's own ``PostgresSettings``. Tables
are created in the runtime schema via ``search_path``, matching the rest of
the application.
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
from typing import Iterator

from psycopg.conninfo import make_conninfo

from database.postgres_config import PostgresSettings


def build_conninfo(settings: PostgresSettings | None = None) -> str:
    resolved = settings or PostgresSettings.from_env()
    kwargs = dict(resolved.connection_kwargs())
    options = f"-csearch_path={resolved.runtime_schema},public"
    kwargs["options"] = options
    return make_conninfo(**kwargs)


@contextmanager
def open_checkpointer(settings: PostgresSettings | None = None) -> Iterator[object]:
    """Yield a ready ``PostgresSaver``; the underlying pool closes on exit."""

    from langgraph.checkpoint.postgres import PostgresSaver

    conninfo = build_conninfo(settings)
    with PostgresSaver.from_conn_string(conninfo) as saver:
        saver.setup()
        yield saver


@contextmanager
def open_store(settings: PostgresSettings | None = None) -> Iterator[object]:
    """Yield a ready ``PostgresStore`` for long-term memory."""

    from langgraph.store.postgres import PostgresStore

    conninfo = build_conninfo(settings)
    with PostgresStore.from_conn_string(conninfo) as store:
        store.setup()
        yield store


@contextmanager
def open_persistence(settings: PostgresSettings | None = None) -> Iterator[tuple[object, object | None]]:
    """Yield ``(checkpointer, store)`` with one shared lifecycle."""

    with ExitStack() as stack:
        checkpointer = stack.enter_context(open_checkpointer(settings))
        try:
            store = stack.enter_context(open_store(settings))
        except Exception:  # noqa: BLE001 - store is optional for runs
            store = None
        yield checkpointer, store


__all__ = ["build_conninfo", "open_checkpointer", "open_store", "open_persistence"]
