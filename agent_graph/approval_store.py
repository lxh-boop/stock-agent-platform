"""Durable approvals bind one authenticated owner and one checkpoint interrupt set.

The connection-level lock intentionally survives intermediate commits: submitting
a Task uses another connection, and a retry must find that Task after a crash.
No database work happens when the store is constructed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from core.user_scope import current_owner, validate_owner
from database.connection import get_connection
from database.postgres_config import PostgresSettings

_STATES = {'pending', 'queued', 'running', 'retryable', 'blocked', 'completed', 'superseded', 'expired'}


def _json(value: Any, *, max_bytes: int = 262144) -> str:
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(result.encode('utf-8')) > max_bytes:
        raise ValueError('approval_payload_too_large')
    return result


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _scope(user_id: str, session_id: str = '', *, require_session: bool = True) -> tuple[str, str]:
    user_id = validate_owner(user_id)
    if current_owner() and current_owner() != user_id:
        raise PermissionError('pending_action_owner_mismatch')
    if not isinstance(session_id, str) or len(session_id) > 200 or (require_session and not session_id.strip()):
        raise ValueError('approval_session_required')
    return user_id, session_id


def thread_id_for_run(user_id: str, session_id: str, run_id: str) -> str:
    user_id, session_id = _scope(user_id, session_id)
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 200:
        raise ValueError('approval_run_required')
    return 'agent_run_' + _digest([user_id, session_id, run_id])


def _interrupts(value: list[dict]) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise ValueError('approval_interrupts_required')
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or len(item['id']) > 200:
            raise ValueError('approval_interrupt_id_required')
        if not isinstance(item.get('value'), dict):
            raise ValueError('approval_interrupt_payload_required')
        result.append({'id': item['id'], 'value': item['value']})
    if len({item['id'] for item in result}) != len(result):
        raise ValueError('duplicate_approval_interrupt')
    return json.loads(_json(sorted(result, key=lambda item: item['id'])))


def interrupt_signature(interrupts: list[dict]) -> str:
    return _digest(_interrupts(interrupts))


def _table():
    from psycopg import sql
    return sql.Identifier(PostgresSettings.from_env().runtime_schema, 'agent_tool_approval')


def _ensure(conn):
    from psycopg import sql
    table = _table()
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('agent-tool-approval-schema',))
    conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
        approval_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
        run_id TEXT NOT NULL, thread_id TEXT NOT NULL, interrupts JSONB NOT NULL,
        payload_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        phase TEXT NOT NULL DEFAULT 'pending', decision TEXT NOT NULL DEFAULT '',
        task_id TEXT NOT NULL DEFAULT '', request_id TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL DEFAULT '', result JSONB, error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at TIMESTAMPTZ NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '24 hours'))''').format(table))
    conn.execute(sql.SQL('CREATE INDEX IF NOT EXISTS agent_tool_approval_owner ON {} (user_id, session_id, updated_at DESC)').format(table))
    conn.commit()
    return table


def _record(row: dict) -> dict:
    return {key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}


def _expire(conn, table, user_id: str):
    from psycopg import sql
    conn.execute(sql.SQL("UPDATE {} SET state='expired',updated_at=CURRENT_TIMESTAMP WHERE user_id=%s AND state IN ('pending','retryable') AND expires_at<=CURRENT_TIMESTAMP").format(table), (user_id,))


class _LockedApproval:
    def __init__(self, conn, table, row):
        self.conn, self.table, self.row = conn, table, _record(row)

    def update(self, **fields) -> dict:
        from psycopg import sql
        from psycopg.types.json import Jsonb
        allowed = {'state', 'phase', 'decision', 'task_id', 'request_id', 'idempotency_key', 'result', 'error'}
        if not fields or set(fields) - allowed:
            raise ValueError('invalid_approval_update')
        if 'state' in fields and fields['state'] not in _STATES:
            raise ValueError('invalid_approval_state')
        if 'decision' in fields:
            if fields['decision'] not in {'approved', 'rejected'}:
                raise ValueError('invalid_approval_decision')
            if self.row['decision'] and self.row['decision'] != fields['decision']:
                raise ValueError('approval_decision_already_bound')
        if self.row['state'] in {'completed', 'superseded', 'expired'}:
            if any(self.row.get(key) != value for key, value in fields.items()):
                raise ValueError('approval_already_closed')
            return self.row
        for key, value in fields.items():
            if key != 'result' and (not isinstance(value, str) or len(value) > (2000 if key == 'error' else 300)):
                raise ValueError('invalid_approval_update')
        values = [Jsonb(json.loads(_json(value, max_bytes=1048576))) if key == 'result' and value is not None else value for key, value in fields.items()]
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in fields)
        row = self.conn.execute(sql.SQL('UPDATE {} SET {},updated_at=CURRENT_TIMESTAMP WHERE approval_id=%s AND user_id=%s AND session_id=%s RETURNING *').format(self.table, assignments),
            (*values, self.row['approval_id'], self.row['user_id'], self.row['session_id'])).fetchone()
        self.conn.commit()
        self.row = _record(row)
        return self.row

    def latest_task(self) -> dict | None:
        from psycopg import sql
        schema = PostgresSettings.from_env().runtime_schema
        if not self.conn.execute('SELECT to_regclass(%s) AS name', (schema + '.task_runs',)).fetchone()['name']:
            self.conn.commit()
            return None
        row = self.conn.execute(sql.SQL('''SELECT task_id,status FROM {} WHERE owner_id=%s AND session_id=%s
            AND task_type='agent.resume' AND metadata_json::jsonb->>'approval_id'=%s
            ORDER BY created_at DESC,task_id DESC LIMIT 1''').format(sql.Identifier(schema, 'task_runs')),
            (self.row['user_id'], self.row['session_id'], self.row['approval_id'])).fetchone()
        self.conn.commit()
        return dict(row) if row else None


class ApprovalStore:
    def save_pending(self, *, user_id: str, session_id: str, run_id: str,
                     thread_id: str, interrupts: list[dict]) -> dict:
        from psycopg import sql
        from psycopg.types.json import Jsonb
        user_id, session_id = _scope(user_id, session_id)
        if thread_id != thread_id_for_run(user_id, session_id, run_id):
            raise ValueError('approval_thread_mismatch')
        pending = _interrupts(interrupts)
        signature = interrupt_signature(pending)
        ident = 'lg_approve_' + _digest([user_id, session_id, run_id, signature])
        with get_connection(runtime=True) as conn:
            table = _ensure(conn)
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('approval-save:' + thread_id,))
            # Repeated serialization of the same checkpoint cannot reset an
            # approval or supersede a newer set of interrupts.
            existing = conn.execute(sql.SQL('SELECT * FROM {} WHERE approval_id=%s').format(table), (ident,)).fetchone()
            if existing:
                return _record(existing)
            conn.execute(sql.SQL("UPDATE {} SET state='superseded',updated_at=CURRENT_TIMESTAMP WHERE user_id=%s AND session_id=%s AND run_id=%s AND state IN ('pending','retryable')").format(table), (user_id, session_id, run_id))
            row = conn.execute(sql.SQL('''INSERT INTO {} (approval_id,user_id,session_id,run_id,thread_id,interrupts,payload_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''').format(table),
                (ident, user_id, session_id, run_id, thread_id, Jsonb(pending), signature)).fetchone()
            conn.commit()
            return _record(row)

    def list_pending(self, *, user_id: str, session_id: str = '') -> list[dict]:
        from psycopg import sql
        user_id, session_id = _scope(user_id, session_id, require_session=False)
        with get_connection(runtime=True) as conn:
            table = _ensure(conn)
            _expire(conn, table, user_id)
            rows = conn.execute(sql.SQL("SELECT * FROM {} WHERE user_id=%s AND (%s='' OR session_id=%s) AND state NOT IN ('completed','superseded','expired') ORDER BY updated_at DESC,approval_id LIMIT 100").format(table), (user_id, session_id, session_id)).fetchall()
            conn.commit()
            return [_record(row) for row in rows]

    def get(self, approval_id: str, *, user_id: str, session_id: str) -> dict:
        from psycopg import sql
        user_id, session_id = _scope(user_id, session_id)
        with get_connection(runtime=True) as conn:
            table = _ensure(conn)
            _expire(conn, table, user_id)
            row = conn.execute(sql.SQL('SELECT * FROM {} WHERE approval_id=%s AND user_id=%s AND session_id=%s').format(table), (approval_id, user_id, session_id)).fetchone()
            conn.commit()
            if not row:
                raise KeyError('pending_action_not_found')
            return _record(row)

    @contextmanager
    def locked(self, approval_id: str, *, user_id: str, session_id: str):
        from psycopg import sql
        user_id, session_id = _scope(user_id, session_id)
        with get_connection(runtime=True) as conn:
            table = _ensure(conn)
            key = 'approval-control:' + str(approval_id)
            conn.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', (key,))
            conn.commit()
            try:
                _expire(conn, table, user_id)
                row = conn.execute(sql.SQL('SELECT * FROM {} WHERE approval_id=%s AND user_id=%s AND session_id=%s').format(table), (approval_id, user_id, session_id)).fetchone()
                conn.commit()
                if not row:
                    raise KeyError('pending_action_not_found')
                if row['state'] == 'expired':
                    raise ValueError('approval_expired_create_new_request')
                yield _LockedApproval(conn, table, row)
            finally:
                conn.rollback()
                conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))
                conn.commit()
