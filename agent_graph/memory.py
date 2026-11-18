"""LangGraph PostgreSQL checkpointer assembly.

The checkpointer exists for exactly one reason: persisting interrupt/approval
state so pending-actions resume works across page reloads. The thread ID binds
the authenticated owner, session and run. Tables live in the runtime schema via
``search_path``. Explicit research notes use a separate personal repository;
conversation-history summarization from the legacy runtime is not restored.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from psycopg.conninfo import make_conninfo

from database.postgres_config import PostgresSettings


def build_conninfo(settings: PostgresSettings | None = None) -> str:
    resolved = settings or PostgresSettings.from_env()
    kwargs = dict(resolved.connection_kwargs())
    kwargs["options"] = f"-csearch_path={resolved.runtime_schema},public"
    return make_conninfo(**kwargs)


@contextmanager
def open_checkpointer(settings: PostgresSettings | None = None) -> Iterator[object]:
    """Yield a ready ``PostgresSaver``; the underlying pool closes on exit."""

    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(build_conninfo(settings)) as saver:
        saver.setup()
        yield saver


__all__ = ["build_conninfo", "open_checkpointer"]
