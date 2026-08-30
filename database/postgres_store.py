from __future__ import annotations

from contextlib import contextmanager
import re
from typing import Any, Iterator

from database.connection import get_connection
from database.postgres_config import PostgresSettings
from database.table_registry import primary_key_for

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(value: str) -> str:
    text = str(value or "")
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"unsafe_sql_identifier:{text}")
    return f'"{text}"'


def deterministic_order_columns(table: str, order_by: str | None) -> tuple[str, ...]:
    if not order_by:
        return ()
    keys = tuple(primary_key_for(table))
    return (str(order_by),) + tuple(name for name in keys if name != str(order_by))


class PostgresStore:
    """Single authoritative PostgreSQL CRUD boundary.

    PostgreSQL is the single authoritative read/write boundary.
    """

    def __init__(self, *, runtime: bool = False) -> None:
        self.runtime = bool(runtime)
        self.settings = PostgresSettings.from_env()

    def _connect(self):
        return get_connection(runtime=self.runtime)

    @contextmanager
    def transaction(self) -> Iterator[object]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _execute(conn, query: str, params: list[Any] | tuple[Any, ...] = ()):
        return conn.execute(query, params)

    def insert(
        self,
        table: str,
        record: dict[str, Any],
        replace: bool = False,
        *,
        connection=None,
    ) -> dict[str, Any]:
        if not record:
            raise ValueError("record cannot be empty")
        if replace:
            return self.upsert(table, record, connection=connection)
        columns = list(record)
        query = (
            f"INSERT INTO {quote_identifier(table)} "
            f"({', '.join(quote_identifier(c) for c in columns)}) "
            f"VALUES ({', '.join('%s' for _ in columns)}) RETURNING *"
        )
        own = connection is None
        conn = connection or self._connect()
        try:
            row = conn.execute(query, [record[c] for c in columns]).fetchone()
            if own:
                conn.commit()
            return dict(row or record)
        except Exception:
            if own:
                conn.rollback()
            raise
        finally:
            if own:
                conn.close()

    def upsert(self, table: str, record: dict[str, Any], *, connection=None) -> dict[str, Any]:
        if not record:
            raise ValueError("record cannot be empty")
        pk_columns = tuple(primary_key_for(table))
        if not pk_columns or not all(name in record for name in pk_columns):
            raise ValueError(f"upsert into {table} requires primary key {pk_columns}")
        columns = list(record)
        update_columns = [name for name in columns if name not in pk_columns]
        query = (
            f"INSERT INTO {quote_identifier(table)} "
            f"({', '.join(quote_identifier(c) for c in columns)}) "
            f"VALUES ({', '.join('%s' for _ in columns)}) "
            f"ON CONFLICT ({', '.join(quote_identifier(c) for c in pk_columns)}) "
        )
        if update_columns:
            query += "DO UPDATE SET " + ", ".join(
                f"{quote_identifier(c)}=EXCLUDED.{quote_identifier(c)}" for c in update_columns
            )
        else:
            query += "DO NOTHING"
        query += " RETURNING *"
        own = connection is None
        conn = connection or self._connect()
        try:
            row = conn.execute(query, [record[c] for c in columns]).fetchone()
            if row is None:
                row = self.get(table, {k: record[k] for k in pk_columns}, connection=conn)
            if own:
                conn.commit()
            return dict(row or record)
        except Exception:
            if own:
                conn.rollback()
            raise
        finally:
            if own:
                conn.close()

    def get(self, table: str, key: dict[str, Any], *, connection=None) -> dict[str, Any] | None:
        if not key:
            raise ValueError("key cannot be empty")
        query = (
            f"SELECT * FROM {quote_identifier(table)} WHERE "
            + " AND ".join(f"{quote_identifier(c)}=%s" for c in key)
            + " LIMIT 1"
        )
        own = connection is None
        conn = connection or self._connect()
        try:
            row = conn.execute(query, list(key.values())).fetchone()
            return dict(row) if row is not None else None
        finally:
            if own:
                conn.close()

    def list(
        self,
        table: str,
        filters: dict[str, Any] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int | None = None,
        *,
        connection=None,
    ) -> list[dict[str, Any]]:
        filters = dict(filters or {})
        query = f"SELECT * FROM {quote_identifier(table)}"
        params: list[Any] = []
        if filters:
            query += " WHERE " + " AND ".join(f"{quote_identifier(c)}=%s" for c in filters)
            params.extend(filters.values())
        if order_by:
            direction = "DESC NULLS LAST" if descending else "ASC NULLS FIRST"
            query += " ORDER BY " + ", ".join(
                f"{quote_identifier(c)} {direction}"
                for c in deterministic_order_columns(table, order_by)
            )
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        if offset is not None:
            query += " OFFSET %s"
            params.append(max(0, int(offset)))
        own = connection is None
        conn = connection or self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            if own:
                conn.close()

    def list_by_values(
        self,
        table: str,
        column: str,
        values: list[Any],
        filters: dict[str, Any] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        *,
        connection=None,
    ) -> list[dict[str, Any]]:
        if not values:
            return []
        filters = dict(filters or {})
        params: list[Any] = list(values)
        predicates = [
            f"{quote_identifier(column)} IN ({', '.join('%s' for _ in values)})"
        ]
        for name, value in filters.items():
            predicates.append(f"{quote_identifier(name)}=%s")
            params.append(value)
        query = f"SELECT * FROM {quote_identifier(table)} WHERE {' AND '.join(predicates)}"
        if order_by:
            direction = "DESC NULLS LAST" if descending else "ASC NULLS FIRST"
            query += " ORDER BY " + ", ".join(
                f"{quote_identifier(c)} {direction}"
                for c in deterministic_order_columns(table, order_by)
            )
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(0, int(limit)))
        own = connection is None
        conn = connection or self._connect()
        try:
            return [dict(row) for row in conn.execute(query, params).fetchall()]
        finally:
            if own:
                conn.close()

    def update(
        self,
        table: str,
        key: dict[str, Any],
        changes: dict[str, Any],
        *,
        connection=None,
    ) -> int:
        if not key:
            raise ValueError("key cannot be empty")
        if not changes:
            return 0
        params = list(changes.values()) + list(key.values())
        query = (
            f"UPDATE {quote_identifier(table)} SET "
            + ", ".join(f"{quote_identifier(c)}=%s" for c in changes)
            + " WHERE "
            + " AND ".join(f"{quote_identifier(c)}=%s" for c in key)
        )
        own = connection is None
        conn = connection or self._connect()
        try:
            cur = conn.execute(query, params)
            if own:
                conn.commit()
            return int(cur.rowcount or 0)
        except Exception:
            if own:
                conn.rollback()
            raise
        finally:
            if own:
                conn.close()

    def delete(self, table: str, key: dict[str, Any], *, connection=None) -> int:
        return self.delete_where(table, key, connection=connection)

    def delete_where(self, table: str, filters: dict[str, Any], *, connection=None) -> int:
        if not filters:
            raise ValueError("delete filters cannot be empty")
        query = (
            f"DELETE FROM {quote_identifier(table)} WHERE "
            + " AND ".join(f"{quote_identifier(c)}=%s" for c in filters)
        )
        own = connection is None
        conn = connection or self._connect()
        try:
            cur = conn.execute(query, list(filters.values()))
            if own:
                conn.commit()
            return int(cur.rowcount or 0)
        except Exception:
            if own:
                conn.rollback()
            raise
        finally:
            if own:
                conn.close()

    def fetch_all(self, query: str, params: list[Any] | tuple[Any, ...] = (), *, connection=None) -> list[dict[str, Any]]:
        own = connection is None
        conn = connection or self._connect()
        try:
            return [dict(row) for row in conn.execute(query, params).fetchall()]
        finally:
            if own:
                conn.close()

    def fetch_one(self, query: str, params: list[Any] | tuple[Any, ...] = (), *, connection=None) -> dict[str, Any] | None:
        own = connection is None
        conn = connection or self._connect()
        try:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row is not None else None
        finally:
            if own:
                conn.close()

    def execute(self, query: str, params: list[Any] | tuple[Any, ...] = (), *, connection=None) -> int:
        own = connection is None
        conn = connection or self._connect()
        try:
            cur = conn.execute(query, params)
            if own:
                conn.commit()
            return int(cur.rowcount or 0)
        except Exception:
            if own:
                conn.rollback()
            raise
        finally:
            if own:
                conn.close()
