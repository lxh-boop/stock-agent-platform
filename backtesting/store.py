"""只读个人 Alpha158 的流式快照；已完成回测独立保存，不随五年因子清理。"""
from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Iterator
from uuid import UUID

from backtesting.contracts import BacktestSpec, DataSpec, AnalysisSpec, ScreenSpec, MAX_STOCKS, ROW_KINDS, INDEX_CODES, INDEX_FIELDS
from core.alpha158_contract import FEATURE_COLUMNS, feature_version
from core.user_scope import current_owner, user_schema
from database.connection import get_connection, transaction


def _identity():
    owner = current_owner()
    if not owner:
        raise ValueError('请先登录后运行个人回测')
    return owner, user_schema(owner)


def _tables():
    from psycopg import sql
    _, schema = _identity()
    return (sql.Identifier(schema, 'alpha158_daily'), sql.Identifier(schema, 'alpha158_state'))


def _coverage(conn):
    from psycopg import sql
    _, schema = _identity()
    exists = conn.execute('SELECT to_regclass(%s) AS t', (schema + '.alpha158_state',)).fetchone()['t']
    if not exists:
        return None
    table, state = _tables()
    version = feature_version()
    meta = conn.execute(sql.SQL('SELECT * FROM {} WHERE feature_version=%s').format(state), (version,)).fetchone()
    if not meta:
        return None
    span = conn.execute(sql.SQL('''SELECT MIN(trade_date) AS start_date, MAX(trade_date) AS end_date,
        COUNT(DISTINCT stock_code) AS stock_count FROM {} WHERE feature_version=%s''').format(table), (version,)).fetchone()
    if not span['start_date']:
        return None
    return {'feature_version': version, 'start_date': str(span['start_date']),
            'end_date': str(span['end_date']), 'stock_count': span['stock_count'],
            'updated_at': meta['updated_at'].isoformat()}


def catalog() -> dict:
    _identity()
    with get_connection() as conn:
        coverage = _coverage(conn)
    return {'fields': list(FEATURE_COLUMNS), 'coverage': coverage,
            'max_stocks': MAX_STOCKS, 'max_positions': 50, 'indexes': INDEX_CODES, 'index_fields': INDEX_FIELDS,
            'schemas': {k: m.model_json_schema() for k, m in [('backtest', BacktestSpec), ('analysis', AnalysisSpec), ('screen', ScreenSpec)]},
            'message': '' if coverage else '个人因子库为空或版本未更新，请先运行一次我的日更。'}


@contextmanager
def snapshot(spec: DataSpec) -> Iterator[tuple[dict, Iterator]]:
    """一个 REPEATABLE READ 快照覆盖整个回测，日更中途修改/清理不改变输入。"""
    from psycopg import sql
    owner, _ = _identity()
    with get_connection() as conn:
        conn.commit()  # 用户schema初始化和search_path设置先提交，再开始纯读取快照。
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        conn.execute("SET LOCAL statement_timeout = '120s'")
        meta = _coverage(conn)
        if not meta:
            raise ValueError('个人因子库为空或版本未更新，请先运行我的日更')
        if str(spec.start_date) < meta['start_date'] or str(spec.end_date) > meta['end_date']:
            raise ValueError(f"回测区间超出本人可用数据：{meta['start_date']} 至 {meta['end_date']}")
        table, _ = _tables()
        where = sql.SQL('feature_version=%s AND trade_date BETWEEN %s AND %s')
        params = [meta['feature_version'], spec.start_date, spec.end_date]
        universe = spec.universe()
        if universe is not None:
            where += sql.SQL(' AND stock_code=ANY(%s)')
            params.append(universe)
        count = conn.execute(sql.SQL('SELECT COUNT(DISTINCT stock_code) AS n FROM {} WHERE {}').format(table, where), params).fetchone()['n']
        if count == 0:
            raise ValueError('所选股票池和日期没有因子数据')
        if count > MAX_STOCKS:
            raise ValueError(f'当前回测股票池上限 {MAX_STOCKS}，请明确缩小股票范围')
        if universe is not None and count < len(universe):
            raise ValueError('部分指定股票在区间内没有个人因子数据，不能静默忽略')
        # 在同一快照中取得已保存的指数；行情/指标/因子均不跨账号。
        from backtesting.market import read_snapshot
        markets, sources = read_snapshot(conn, spec)
        prior = conn.execute(sql.SQL('SELECT DISTINCT trade_date FROM {} WHERE feature_version=%s AND trade_date < %s ORDER BY trade_date DESC LIMIT %s').format(table), (meta['feature_version'], spec.start_date, spec.warmup_days())).fetchall()
        if prior:
            params[1] = min(r['trade_date'] for r in prior)
        # 不读取全部158列或全部五年DataFrame；只取本次所需列并按天交给纯引擎。
        price_cols = sql.SQL(', ').join(sql.SQL("source_payload ->> {} AS {}").format(
            sql.Literal(c), sql.Identifier(c)) for c in ('open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'volume', 'adj_factor'))
        feature_cols = sql.SQL(', ').join(map(sql.Identifier, spec.fields()))
        query = sql.SQL('''SELECT trade_date, stock_code, input_hash, {}, {} FROM {} WHERE {}
                          ORDER BY trade_date, stock_code''').format(price_cols, feature_cols, table, where)
        with conn.cursor(name='strategy_backtest_snapshot') as cursor:
            cursor.execute(query, params)

            def day_rows():
                day, rows = None, {}
                while chunk := cursor.fetchmany(1000):
                    if current_owner() != owner:
                        raise ValueError('回测用户上下文发生变化')
                    for record in chunk:
                        current = record['trade_date']
                        if day is not None and current != day:
                            yield day, rows
                            rows = {}
                        day = current
                        payload = {k: record[k] for k in ('input_hash', 'open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'volume', 'adj_factor')}
                        # 非有限因子作为缺失值，不序列化 NaN/Infinity。
                        from backtesting.engine import finite
                        payload.update({k: finite(record[k]) for k in spec.fields()})
                        rows[record['stock_code']] = payload
                if day is not None:
                    yield day, rows
            yield {**meta, 'selected_stock_count': count, 'isolation': 'repeatable_read',
                   'index_sources': sources, '_markets': markets, 'warmup_from': str(params[1])}, day_rows()


def _reports(conn):
    from psycopg import sql
    _, schema = _identity()
    table = sql.Identifier(schema, 'backtest_run')
    # 首次建表的多进程竞争使用事务锁，已完成报告不兼容旧latest文件。
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('backtests:' + schema,))
    conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
        run_id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        report JSONB NOT NULL)''').format(table))
    return table


def save_report(run_id: str, report: dict) -> None:
    from psycopg import sql
    from psycopg.types.json import Jsonb
    with transaction() as conn:
        table = _reports(conn)
        conn.execute(sql.SQL('''INSERT INTO {} (run_id, report) VALUES (%s,%s)
            ON CONFLICT (run_id) DO NOTHING''').format(table),
            (UUID(run_id), Jsonb(report, dumps=lambda x: json.dumps(x, ensure_ascii=False, allow_nan=False))))


def get_report(run_id: str, *, summary: bool = False) -> dict:
    from psycopg import sql
    ident = UUID(run_id)
    with transaction() as conn:
        table = _reports(conn)
        expression = sql.SQL('report - %s::text[]') if summary else sql.SQL('report')
        params = (list(ROW_KINDS), ident) if summary else (ident,)
        row = conn.execute(sql.SQL('SELECT {} AS report, created_at FROM {} WHERE run_id=%s').format(expression, table), params).fetchone()
    if not row:
        raise KeyError('回测不存在或不属于当前用户')
    return {**row['report'], 'run_id': str(ident), 'created_at': row['created_at'].isoformat()}


def list_reports(limit: int = 30, offset: int = 0, kind: str | None = None) -> dict:
    from psycopg import sql
    with transaction() as conn:
        table = _reports(conn)
        condition = sql.SQL("WHERE report->>'kind'=%s") if kind else sql.SQL('')
        args = (kind,) if kind else ()
        total = conn.execute(sql.SQL('SELECT COUNT(*) AS n FROM {} {}').format(table, condition), args).fetchone()['n']
        rows = conn.execute(sql.SQL('''SELECT run_id, created_at, report->'spec' AS spec,
            report->'metrics' AS metrics, report->>'kind' AS kind, report->>'engine_version' AS engine_version
            FROM {} {} ORDER BY created_at DESC, run_id LIMIT %s OFFSET %s''').format(table, condition), (*args, limit, offset)).fetchall()
    return {'records': [{**r, 'run_id': str(r['run_id']), 'created_at': r['created_at'].isoformat()} for r in rows], 'total': total}


def report_rows(run_id: str, kind: str, limit: int = 50, offset: int = 0, factor: str | None = None) -> dict:
    """分页和可选因子过滤在数据库执行，不把全量持仓搬到API进程切片。"""
    from psycopg import sql
    if kind not in ROW_KINDS or not 1 <= limit <= 1500 or offset < 0:
        raise ValueError('回测分页参数无效')
    if factor is not None and factor not in FEATURE_COLUMNS:
        raise ValueError('未知因子')
    with transaction() as conn:
        table = _reports(conn)
        row = conn.execute(sql.SQL("""WITH source AS (SELECT report FROM {} WHERE run_id=%s),
            items AS (SELECT value, ordinal FROM source,
                jsonb_array_elements(COALESCE(report->%s, '[]')) WITH ORDINALITY AS part(value,ordinal)
                WHERE (%s::text IS NULL OR value->>'factor'=%s)),
            page AS (SELECT value, ordinal FROM items ORDER BY ordinal LIMIT %s OFFSET %s)
            SELECT (SELECT COUNT(*) FROM items) AS total,
                COALESCE((SELECT jsonb_agg(value ORDER BY ordinal) FROM page), '[]') AS records,
                EXISTS(SELECT 1 FROM source) AS found""").format(table),
            (UUID(run_id), kind, factor, factor, limit, offset)).fetchone()
    if not row['found']: raise KeyError('回测不存在或不属于当前用户')
    return {'records': row['records'], 'total': row['total'], 'offset': offset, 'limit': limit}


def delete_report(run_id: str) -> None:
    from psycopg import sql
    with transaction() as conn:
        table = _reports(conn)
        if conn.execute(sql.SQL('DELETE FROM {} WHERE run_id=%s').format(table), (UUID(run_id),)).rowcount == 0:
            raise KeyError('回测不存在或不属于当前用户')


def _strategies(conn):
    from psycopg import sql
    _, schema = _identity()
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('strategies:' + schema,))
    table = sql.Identifier(schema, 'backtest_strategy')
    conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (strategy_id UUID PRIMARY KEY,
        spec JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''').format(table))
    return table


def save_strategy(strategy_id: str, spec: dict) -> dict:
    from psycopg import sql
    from psycopg.types.json import Jsonb
    normalized = BacktestSpec.model_validate(spec).model_dump(mode='json')
    with transaction() as conn:
        table = _strategies(conn)
        conn.execute(sql.SQL('''INSERT INTO {} (strategy_id,spec) VALUES (%s,%s) ON CONFLICT(strategy_id)
            DO UPDATE SET spec=EXCLUDED.spec,updated_at=CURRENT_TIMESTAMP''').format(table), (UUID(strategy_id), Jsonb(normalized)))
    return {'strategy_id': strategy_id, 'spec': normalized}


def list_strategies(limit: int = 30, offset: int = 0) -> dict:
    from psycopg import sql
    with transaction() as conn:
        table = _strategies(conn)
        rows = conn.execute(sql.SQL('SELECT * FROM {} ORDER BY updated_at DESC LIMIT %s OFFSET %s').format(table), (limit, offset)).fetchall()
        count = conn.execute(sql.SQL('SELECT COUNT(*) AS n FROM {}').format(table)).fetchone()['n']
    return {'records': [{**r, 'strategy_id': str(r['strategy_id']), 'updated_at': r['updated_at'].isoformat()} for r in rows], 'total': count}


def delete_strategy(strategy_id: str) -> None:
    from psycopg import sql
    with transaction() as conn:
        table = _strategies(conn)
        if conn.execute(sql.SQL('DELETE FROM {} WHERE strategy_id=%s').format(table), (UUID(strategy_id),)).rowcount == 0:
            raise KeyError('策略不存在或不属于当前用户')


@contextmanager
def execution_lock():
    """页面和Agent也共享执行锁，同一用户不能绕过任务入口并发占满内存。"""
    owner, _ = _identity()
    with get_connection(runtime=True) as conn:
        row = conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired', ('research-run:' + owner,)).fetchone()
        conn.commit()
        if not row['acquired']:
            raise ValueError('本人已有研究任务正在计算，请先完成或取消它')
        try:
            yield
        finally:
            conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 0))', ('research-run:' + owner,))
            conn.commit()
