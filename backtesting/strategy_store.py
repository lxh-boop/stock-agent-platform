"""Versioned personal research strategies; never modifies paper accounts or bindings."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from backtesting.contracts import BacktestSpec, ROW_KINDS
from core.user_scope import current_owner, user_schema
from database.connection import transaction

ACTIVE = {'queued', 'running', 'cancelling'}


def table(name):
    owner = current_owner()
    if not owner:
        raise ValueError('请先登录')
    return sql.Identifier(user_schema(owner), name)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def ensure(conn):
    owner = current_owner()
    if not owner:
        raise ValueError('请先登录')
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('strategies:' + user_schema(owner),))
    strategies = table('backtest_strategy')
    conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (strategy_id UUID PRIMARY KEY,
        spec JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''').format(strategies))
    # The deployed table originally contained only the three columns above.
    conn.execute(sql.SQL('''ALTER TABLE {} ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ADD COLUMN IF NOT EXISTS latest_run_id UUID,
        ADD COLUMN IF NOT EXISTS latest_run_version BIGINT,
        ADD COLUMN IF NOT EXISTS active_run_id UUID,
        ADD COLUMN IF NOT EXISTS last_task_id TEXT,
        ADD COLUMN IF NOT EXISTS last_status TEXT NOT NULL DEFAULT 'draft',
        ADD COLUMN IF NOT EXISTS last_error TEXT''').format(strategies))
    conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
        strategy_id UUID NOT NULL, version BIGINT NOT NULL, spec JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(strategy_id,version))''').format(table('backtest_strategy_revision')))
    conn.execute(sql.SQL('''INSERT INTO {} (strategy_id,version,spec)
        SELECT strategy_id,version,spec FROM {} ON CONFLICT DO NOTHING''').format(table('backtest_strategy_revision'), strategies))
    # No cascading FK: deleting a strategy must not delete research reports/history.
    conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
        run_id UUID PRIMARY KEY, strategy_id UUID NOT NULL, version BIGINT NOT NULL,
        task_id TEXT, status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''').format(table('backtest_strategy_run')))
    conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
        request_id UUID PRIMARY KEY, operation TEXT NOT NULL, payload_hash TEXT NOT NULL,
        strategy_id UUID NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''').format(table('backtest_strategy_request')))
    return strategies


def receipt(conn, request_id, operation, payload, strategy_id):
    """Reserve a mutation receipt in the same transaction as its effects."""
    ident = UUID(request_id)
    row = conn.execute(sql.SQL('SELECT * FROM {} WHERE request_id=%s').format(table('backtest_strategy_request')), (ident,)).fetchone()
    hashed = digest(payload)
    if row:
        if row['operation'] != operation or row['payload_hash'] != hashed or str(row['strategy_id']) != strategy_id:
            raise ValueError('此请求编号已用于不同策略操作，请创建新请求')
        return True
    conn.execute(sql.SQL('INSERT INTO {} (request_id,operation,payload_hash,strategy_id) VALUES (%s,%s,%s,%s)').format(table('backtest_strategy_request')), (ident, operation, hashed, UUID(strategy_id)))
    return False


def _row(conn, strategy_id):
    row = conn.execute(sql.SQL('SELECT * FROM {} WHERE strategy_id=%s').format(table('backtest_strategy')), (UUID(strategy_id),)).fetchone()
    if not row:
        raise KeyError('策略不存在或不属于当前用户')
    return row


def save(strategy_id, spec, *, request_id=None, expected_version=None, require_version=False):
    normalized = BacktestSpec.model_validate(spec).model_dump(mode='json')
    ident = str(UUID(strategy_id))
    with transaction() as conn:
        strategies = ensure(conn)
        if request_id and receipt(conn, request_id, 'save', {'spec': normalized, 'expected_version': expected_version}, ident):
            return ident
        existing = conn.execute(sql.SQL('SELECT * FROM {} WHERE strategy_id=%s').format(strategies), (UUID(ident),)).fetchone()
        if not existing and conn.execute(sql.SQL('SELECT 1 FROM {} WHERE strategy_id=%s LIMIT 1').format(table('backtest_strategy_revision')), (UUID(ident),)).fetchone():
            raise ValueError('该策略编号已经归档，请使用新编号创建策略')
        if existing and existing['active_run_id']:
            raise ValueError('策略回测正在运行，请先完成或取消任务')
        if existing and require_version and expected_version is None:
            raise ValueError('请先读取策略当前版本，再确认修改')
        if expected_version is not None and (not existing or existing['version'] != expected_version):
            raise ValueError('策略版本已变化，请刷新后重试')
        changed = not existing or existing['spec'] != normalized
        version = 1 if not existing else existing['version'] + int(changed)
        conn.execute(sql.SQL('''INSERT INTO {} AS current (strategy_id,spec,version) VALUES (%s,%s,%s)
            ON CONFLICT(strategy_id) DO UPDATE SET spec=EXCLUDED.spec,version=EXCLUDED.version,
            updated_at=CURRENT_TIMESTAMP,last_status=CASE WHEN current.spec<>EXCLUDED.spec THEN 'draft' ELSE current.last_status END,
            last_error=CASE WHEN current.spec<>EXCLUDED.spec THEN NULL ELSE current.last_error END''').format(strategies),
            (UUID(ident), Jsonb(normalized), version))
        conn.execute(sql.SQL('INSERT INTO {} (strategy_id,version,spec) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING').format(table('backtest_strategy_revision')), (UUID(ident), version, Jsonb(normalized)))
    return ident


def from_report(run_id, *, request_id, name=None):
    from backtesting.store import _reports
    ident = str(UUID(request_id))
    with transaction() as conn:
        strategies = ensure(conn)
        if receipt(conn, request_id, 'from-report', {'run_id': str(UUID(run_id)), 'name': name}, ident):
            _row(conn, ident)
            return ident
        if conn.execute(sql.SQL('SELECT 1 FROM {} WHERE strategy_id=%s LIMIT 1').format(table('backtest_strategy_revision')), (UUID(ident),)).fetchone():
            raise ValueError('该策略编号已被使用，请创建新的请求编号')
        reports = _reports(conn)
        row = conn.execute(sql.SQL('SELECT report FROM {} WHERE run_id=%s').format(reports), (UUID(run_id),)).fetchone()
        if not row:
            raise KeyError('回测不存在或不属于当前用户')
        report = row['report']
        if report.get('kind') != 'backtest' or not isinstance(report.get('metrics'), dict) or not report.get('equity'):
            raise ValueError('只能保存本人已完成且有真实绩效的回测报告')
        spec = dict(report['spec'])
        if name is not None:
            spec['name'] = name
        spec = BacktestSpec.model_validate(spec).model_dump(mode='json')
        conn.execute(sql.SQL('''INSERT INTO {} (strategy_id,spec,latest_run_id,latest_run_version,last_status)
            VALUES (%s,%s,%s,1,'succeeded')''').format(strategies), (UUID(ident), Jsonb(spec), UUID(run_id)))
        conn.execute(sql.SQL('INSERT INTO {} (strategy_id,version,spec) VALUES (%s,1,%s)').format(table('backtest_strategy_revision')), (UUID(ident), Jsonb(spec)))
        # The report can be deliberately saved as several independent strategies.
        # Its source association is on each strategy; rerun IDs are uniquely bound below.
    return ident


def complete_report(conn, run_id, report, *, promote=False):
    link = conn.execute(sql.SQL('''SELECT r.*,v.spec FROM {} r JOIN {} v
        ON v.strategy_id=r.strategy_id AND v.version=r.version WHERE r.run_id=%s''').format(
            table('backtest_strategy_run'), table('backtest_strategy_revision')), (UUID(run_id),)).fetchone()
    if not link:
        return
    if report.get('kind') != 'backtest' or report.get('spec') != link['spec']:
        raise ValueError('回测报告参数与策略版本不一致')
    if not promote:
        return
    conn.execute(sql.SQL("UPDATE {} SET status='succeeded' WHERE run_id=%s").format(table('backtest_strategy_run')), (UUID(run_id),))
    conn.execute(sql.SQL('''UPDATE {} SET latest_run_id=%s,latest_run_version=%s,
        active_run_id=NULL,last_status='succeeded',last_error=NULL,updated_at=CURRENT_TIMESTAMP
        WHERE strategy_id=%s AND version=%s AND active_run_id=%s''').format(table('backtest_strategy')),
        (UUID(run_id), link['version'], link['strategy_id'], link['version'], UUID(run_id)))


def attach_task(run_id, task):
    """Attach a Task to its already durable strategy/version reservation."""
    from backtesting.store import _reports
    if task.get('owner_id') != current_owner() or task.get('task_type') != 'backtest.run':
        raise KeyError('任务不存在或不属于当前用户')
    with transaction() as conn:
        strategies = ensure(conn)
        link = conn.execute(sql.SQL('SELECT r.*,v.spec FROM {} r JOIN {} v ON v.strategy_id=r.strategy_id AND v.version=r.version WHERE r.run_id=%s').format(
            table('backtest_strategy_run'), table('backtest_strategy_revision')), (UUID(run_id),)).fetchone()
        if not link:
            raise KeyError('策略运行记录不存在')
        request = (task.get('request') or {}).get('kwargs') or {}
        if request.get('run_id') != str(UUID(run_id)) or request.get('spec') != link['spec']:
            raise ValueError('任务参数与策略版本不一致')
        if link['task_id'] and link['task_id'] != task['task_id']:
            raise ValueError('策略运行已绑定其他任务')
        conn.execute(sql.SQL('UPDATE {} SET task_id=%s,status=%s WHERE run_id=%s').format(table('backtest_strategy_run')),
            (task['task_id'], task['status'], UUID(run_id)))
        conn.execute(sql.SQL('UPDATE {} SET last_task_id=%s,last_status=%s WHERE active_run_id=%s').format(strategies),
            (task['task_id'], task['status'], UUID(run_id)))
        row = conn.execute(sql.SQL('SELECT report FROM {} WHERE run_id=%s').format(_reports(conn)), (UUID(run_id),)).fetchone()
        if row and task['status'] == 'succeeded':
            complete_report(conn, run_id, row['report'], promote=True)


def recover_task_id(run_id):
    """Recover dispatch that persisted its Task before recording the association."""
    from database.connection import get_connection
    with get_connection(runtime=True) as conn:
        # TaskStore initializes the runtime table, but a reservation can precede it.
        if not conn.execute("SELECT to_regclass('task_runs') AS name").fetchone()['name']:
            return None
        row = conn.execute("""SELECT task_id FROM task_runs WHERE owner_id=%s
            AND task_type='backtest.run' AND metadata_json::jsonb->>'request_id'=%s
            ORDER BY created_at DESC LIMIT 1""", (current_owner(), str(UUID(run_id)))).fetchone()
        return row['task_id'] if row else None


@contextmanager
def idle_dispatch():
    """Hold dispatch exclusion through recovery's recheck and possible cleanup."""
    from database.connection import get_connection
    if not current_owner():
        raise ValueError('请先登录')
    key = 'strategy-dispatch:' + current_owner()
    with get_connection(runtime=True) as conn:
        acquired = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired', (key,)).fetchone()['acquired']
        conn.commit()
        try:
            yield acquired
        finally:
            if acquired:
                conn.rollback()
                conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))
                conn.commit()


def submit(strategy_id, *, request_id, expected_version, callback):
    """Reserve before dispatch; a crash cannot leave an untracked running Task."""
    from database.connection import get_connection
    if not current_owner():
        raise ValueError('请先登录')
    ident, run_id = str(UUID(strategy_id)), str(UUID(request_id))
    key = 'strategy-dispatch:' + current_owner()
    with get_connection(runtime=True) as dispatch:
        dispatch.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', (key,))
        dispatch.commit()
        try:
            with transaction() as conn:
                strategies = ensure(conn)
                current = _row(conn, ident)
                old = conn.execute(sql.SQL('SELECT * FROM {} WHERE run_id=%s').format(table('backtest_strategy_run')), (UUID(run_id),)).fetchone()
                if old:
                    if str(old['strategy_id']) != ident or (expected_version is not None and old['version'] != expected_version):
                        raise ValueError('此请求编号已用于不同策略版本')
                    version = old['version']
                    if not old['task_id'] and current['version'] != version:
                        raise ValueError('策略版本已变化，请创建新请求')
                    if not old['task_id'] and current['active_run_id'] and str(current['active_run_id']) != run_id:
                        raise ValueError('策略回测正在运行，请先完成或取消任务')
                    spec = conn.execute(sql.SQL('SELECT spec FROM {} WHERE strategy_id=%s AND version=%s').format(table('backtest_strategy_revision')), (UUID(ident), version)).fetchone()['spec']
                else:
                    if expected_version is not None and current['version'] != expected_version:
                        raise ValueError('策略版本已变化，请刷新后重试')
                    if current['active_run_id']:
                        raise ValueError('策略回测正在运行，请先完成或取消任务')
                    version, spec = current['version'], current['spec']
                receipt(conn, run_id, 'rerun', {'version': version}, ident)
                if not old:
                    conn.execute(sql.SQL("INSERT INTO {} (run_id,strategy_id,version,status) VALUES (%s,%s,%s,'queued')").format(table('backtest_strategy_run')), (UUID(run_id), UUID(ident), version))
                if not old or not old['task_id']:
                    conn.execute(sql.SQL("""UPDATE {} SET active_run_id=%s,last_task_id=NULL,last_status='queued',
                        last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE strategy_id=%s""").format(strategies), (UUID(run_id), UUID(ident)))
            # The transaction above commits before a Task is created on another connection.
            # Readers can see the reservation, but cannot edit/delete while dispatch holds its lock.
            task = callback(spec)
            attach_task(run_id, task)
            return task
        finally:
            dispatch.rollback()
            dispatch.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))
            dispatch.commit()


def unfinished():
    with transaction() as conn:
        ensure(conn)
        return conn.execute(sql.SQL('''SELECT r.* FROM {} r JOIN {} s ON s.active_run_id=r.run_id''').format(table('backtest_strategy_run'), table('backtest_strategy'))).fetchall()


def reconcile(run_id, status, *, require_unattached=False):
    from backtesting.store import _reports
    if status not in ACTIVE | {'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'}:
        status = 'interrupted'
    with transaction() as conn:
        strategies = ensure(conn)
        link = conn.execute(sql.SQL('''SELECT r.* FROM {} r JOIN {} s ON s.active_run_id=r.run_id
            WHERE r.run_id=%s''').format(table('backtest_strategy_run'), strategies), (UUID(run_id),)).fetchone()
        if not link or (require_unattached and link['task_id']):
            return
        report = conn.execute(sql.SQL('SELECT report FROM {} WHERE run_id=%s').format(_reports(conn)), (UUID(run_id),)).fetchone()
        if report and status == 'succeeded':
            complete_report(conn, run_id, report['report'], promote=True)
            return
        if status == 'succeeded':
            status = 'failed'
        error = {'failed': '本次回测失败，已保留上次成功报告', 'cancelled': '本次回测已取消',
                 'timed_out': '本次回测超时', 'interrupted': '本次回测已中断，请重新提交'}.get(status)
        conn.execute(sql.SQL('UPDATE {} SET status=%s WHERE run_id=%s').format(table('backtest_strategy_run')), (status, UUID(run_id)))
        conn.execute(sql.SQL('''UPDATE {} SET last_status=%s,last_error=%s,active_run_id=%s
            WHERE strategy_id=%s AND active_run_id=%s''').format(strategies),
            (status, error, UUID(run_id) if status in ACTIVE else None, link['strategy_id'], UUID(run_id)))


def _select():
    return sql.SQL('''SELECT s.*, CASE WHEN r.run_id IS NULL THEN NULL ELSE
        (r.report - %s::text[]) || jsonb_build_object('run_id',r.run_id::text,'created_at',r.created_at,
            'equity',COALESCE((SELECT jsonb_agg(value ORDER BY ordinal)
            FROM jsonb_array_elements(COALESCE(r.report->'equity','[]')) WITH ORDINALITY AS p(value,ordinal)
            WHERE ordinal=1 OR ordinal=jsonb_array_length(r.report->'equity')
               OR mod(ordinal-1,GREATEST(1,CEIL((jsonb_array_length(r.report->'equity')-1)/99.0)::bigint))=0),'[]')) END AS latest_report
        FROM {} s LEFT JOIN {} r ON r.run_id=s.latest_run_id''').format(table('backtest_strategy'), table('backtest_run'))


def serialize(row):
    result = dict(row)
    for key in ('strategy_id', 'latest_run_id', 'active_run_id'):
        result[key] = str(result[key]) if result.get(key) else None
    for key in ('created_at', 'updated_at'):
        result[key] = result[key].isoformat()
    result['status'] = result.pop('last_status')
    result['error'] = result.pop('last_error')
    result['active_task_id'] = result['last_task_id'] if result['active_run_id'] else None
    result['task'] = None
    return result


def get(strategy_id):
    from backtesting.store import _reports
    with transaction() as conn:
        ensure(conn)
        _reports(conn)
        row = conn.execute(_select() + sql.SQL(' WHERE s.strategy_id=%s'), (list(ROW_KINDS), UUID(strategy_id))).fetchone()
        if not row:
            raise KeyError('策略不存在或不属于当前用户')
    return serialize(row)


def listing(limit=30, offset=0):
    from backtesting.store import _reports
    with transaction() as conn:
        strategies = ensure(conn)
        reports = _reports(conn)
        rows = conn.execute(_select() + sql.SQL(' ORDER BY s.updated_at DESC,s.strategy_id LIMIT %s OFFSET %s'), (list(ROW_KINDS), limit, offset)).fetchall()
        stats = conn.execute(sql.SQL('''SELECT COUNT(*) AS total,
            COUNT(*) FILTER(WHERE s.last_status='draft') AS draft,
            COUNT(*) FILTER(WHERE s.last_status IN ('queued','running','cancelling')) AS running,
            COUNT(*) FILTER(WHERE s.last_status='succeeded') AS succeeded,
            COUNT(*) FILTER(WHERE s.last_status IN ('failed','cancelled','timed_out','interrupted')) AS failed,
            AVG(CASE WHEN jsonb_typeof(r.report->'metrics'->'annual_return')='number' THEN (r.report->'metrics'->>'annual_return')::double precision END) AS avg_annual_return,
            AVG(CASE WHEN jsonb_typeof(r.report->'metrics'->'sharpe')='number' THEN (r.report->'metrics'->>'sharpe')::double precision END) AS avg_sharpe
            FROM {} s LEFT JOIN {} r ON r.run_id=s.latest_run_id''').format(strategies, reports)).fetchone()
    return {'records': [serialize(row) for row in rows], 'total': stats['total'], 'stats': dict(stats)}


def delete(strategy_id, *, request_id=None, expected_version=None):
    ident = str(UUID(strategy_id))
    with transaction() as conn:
        strategies = ensure(conn)
        if request_id and receipt(conn, request_id, 'delete', {'expected_version': expected_version}, ident):
            return
        current = _row(conn, ident)
        if current['active_run_id']:
            raise ValueError('策略回测正在运行，请先完成或取消任务')
        if expected_version is not None and current['version'] != expected_version:
            raise ValueError('策略版本已变化，请刷新后重试')
        conn.execute(sql.SQL('DELETE FROM {} WHERE strategy_id=%s').format(strategies), (UUID(ident),))


def detach_report(conn, run_id):
    conn.execute(sql.SQL('''UPDATE {} SET latest_run_id=NULL,latest_run_version=NULL,
        last_status=CASE WHEN last_status='succeeded' THEN 'draft' ELSE last_status END
        WHERE latest_run_id=%s''').format(table('backtest_strategy')), (UUID(run_id),))
