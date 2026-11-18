"""Bounded, read-only research queries with explicit account-schema table names."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from typing import Any

from core.alpha158_contract import FEATURE_COLUMNS, feature_version
from core.user_scope import current_owner, user_schema
from database.connection import get_connection

PRICE_FIELDS = ('open', 'high', 'low', 'close', 'volume', 'amount', 'return_1d', 'volatility_20d', 'turnover_rate')
STOCK_FIELDS = ('stock_name', 'full_name', 'exchange', 'list_date', 'industry', 'is_st', 'market_cap')
_TABLES = frozenset(('alpha158_daily', 'alpha158_state', 'market_data_daily', 'stock_basic', 'factor_catalog'))


class ResearchDataRepository:
    def __init__(self):
        self.owner = current_owner()
        if not self.owner:
            raise ValueError('请先登录后读取本人研究数据')
        self.schema = user_schema(self.owner)

    @contextmanager
    def _read(self):
        if current_owner() != self.owner:
            raise ValueError('研究数据账号上下文不匹配')
        conn = get_connection()
        try:
            if conn.execute('SELECT current_schema() AS name').fetchone()['name'] != self.schema:
                raise ValueError('研究数据必须来自当前账号的数据域')
            conn.commit()
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            conn.execute("SET LOCAL statement_timeout = '15s'")
            yield conn
        finally:
            conn.rollback()
            conn.close()

    def _table(self, name):
        from psycopg import sql
        if name not in _TABLES:
            raise ValueError('数据表不在研究读取白名单中')
        return sql.Identifier(self.schema, name)

    def _exists(self, conn, name):
        self._table(name)
        return bool(conn.execute('SELECT to_regclass(%s) AS name', (self.schema + '.' + name,)).fetchone()['name'])

    def _alpha_coverage(self, conn):
        from psycopg import sql
        if not all(self._exists(conn, name) for name in ('alpha158_daily', 'alpha158_state')):
            return None
        meta = conn.execute(sql.SQL('SELECT latest_signal_date,retention_cutoff FROM {} WHERE feature_version=%s').format(
            self._table('alpha158_state')), (feature_version(),)).fetchone()
        if not meta:
            return None
        row = conn.execute(sql.SQL('''SELECT MIN(trade_date) AS start_date,MAX(trade_date) AS end_date,
            COUNT(*) AS row_count,COUNT(DISTINCT stock_code) AS stock_count FROM {}
            WHERE feature_version=%s AND trade_date BETWEEN %s AND %s''').format(self._table('alpha158_daily')),
            (feature_version(), meta['retention_cutoff'], meta['latest_signal_date'])).fetchone()
        if not row['row_count']:
            return None
        return {**row, 'start_date': str(row['start_date']), 'end_date': str(row['end_date']),
                'feature_version': feature_version()}

    def catalog(self):
        from psycopg import sql
        with self._read() as conn:
            alpha = self._alpha_coverage(conn)
            data = {'alpha158_daily': alpha, 'market_data_daily': None, 'stock_basic': None, 'custom_factors': None}
            if self._exists(conn, 'market_data_daily'):
                row = conn.execute(sql.SQL('''SELECT COUNT(*) AS row_count,COUNT(DISTINCT stock_code) AS stock_count,
                    MIN(replace(trade_date,'-','')) AS start_date,MAX(replace(trade_date,'-','')) AS end_date
                    FROM {}''').format(self._table('market_data_daily'))).fetchone()
                data['market_data_daily'] = row
            if self._exists(conn, 'stock_basic'):
                data['stock_basic'] = conn.execute(sql.SQL('SELECT COUNT(*) AS row_count FROM {}').format(
                    self._table('stock_basic'))).fetchone()
            definitions = self._definitions(conn)
            custom = [row for row in definitions if row['source'] == 'custom']
            data['custom_factors'] = {'definition_count': len(custom), 'row_count': None,
                                      'start_date': alpha['start_date'] if alpha else None,
                                      'end_date': alpha['end_date'] if alpha else None}
            return {'tables': data, 'definitions': definitions}

    def _definitions(self, conn):
        from psycopg import sql
        if not self._exists(conn, 'factor_catalog'):
            return []
        return conn.execute(sql.SQL('''SELECT factor_id,name,expression,description,category,source
            FROM {} ORDER BY source,name_key LIMIT 358''').format(self._table('factor_catalog'))).fetchall()

    def definitions(self):
        with self._read() as conn:
            return self._definitions(conn)

    def read_row(self, table: str, fields: list[str], *, day: date, code: str):
        from psycopg import sql
        allowed = FEATURE_COLUMNS if table == 'alpha158_daily' else PRICE_FIELDS if table == 'market_data_daily' else STOCK_FIELDS if table == 'stock_basic' else ()
        if not fields or len(fields) > 12 or any(field not in allowed for field in fields):
            raise ValueError('读取字段不在该数据表白名单中')
        with self._read() as conn:
            if not self._exists(conn, table):
                return []
            columns = sql.SQL(', ').join(map(sql.Identifier, fields))
            if table == 'alpha158_daily':
                coverage = self._alpha_coverage(conn)
                if not coverage or not coverage['start_date'] <= day.isoformat() <= coverage['end_date']:
                    return []
                query = sql.SQL('SELECT stock_code AS code,trade_date AS date,{} FROM {} WHERE feature_version=%s AND trade_date=%s AND stock_code=%s LIMIT 1').format(columns, self._table(table))
                params = (coverage['feature_version'], day, code)
            elif table == 'market_data_daily':
                query = sql.SQL('SELECT stock_code AS code,trade_date AS date,{} FROM {} WHERE trade_date IN (%s,%s) AND stock_code=%s ORDER BY trade_date LIMIT 1').format(columns, self._table(table))
                params = (day.isoformat(), day.strftime('%Y%m%d'), code)
            else:
                query = sql.SQL('SELECT stock_code AS code,{} FROM {} WHERE stock_code=%s LIMIT 1').format(columns, self._table(table))
                params = (code,)
            return conn.execute(query, params).fetchall()

    def custom_history(self, *, day: date, code: str, fields: list[str], lookback: int):
        """Only permitted numeric OHLCV keys; source_payload itself never leaves the query."""
        from psycopg import sql
        if not fields or any(field not in ('open', 'high', 'low', 'close', 'volume', 'vwap') for field in fields):
            raise ValueError('表达式行情字段不在白名单中')
        if not 0 <= lookback <= 504:
            raise ValueError('自定义因子回看范围超出限制')
        with self._read() as conn:
            coverage = self._alpha_coverage(conn)
            if not coverage or not coverage['start_date'] <= day.isoformat() <= coverage['end_date']:
                return []
            columns = sql.SQL(', ').join(sql.SQL('source_payload ->> {} AS {}').format(sql.Literal(field), sql.Identifier(field)) for field in fields)
            return conn.execute(sql.SQL('''SELECT stock_code AS code,trade_date AS date,{} FROM {}
                WHERE feature_version=%s AND stock_code=%s AND trade_date BETWEEN %s AND %s
                ORDER BY trade_date DESC LIMIT %s''').format(columns, self._table('alpha158_daily')),
                (coverage['feature_version'], code, coverage['start_date'], day, lookback + 1)).fetchall()

    def screen(self, *, as_of: date | None, codes: list[str] | None, filters: list[dict], limit: int):
        from psycopg import sql
        if not 1 <= limit <= 200 or len(filters) > 8 or (codes is not None and len(codes) > 1000):
            raise ValueError('股票池查询范围超出限制')
        operators = {'gt': '>', 'ge': '>=', 'lt': '<', 'le': '<=', 'eq': '='}
        with self._read() as conn:
            coverage = self._alpha_coverage(conn)
            if not coverage:
                return {'as_of': None, 'coverage': None, 'codes': [], 'total': 0, 'returned': 0, 'truncated': False, 'universe_count': 0}
            chosen = conn.execute(sql.SQL('''SELECT MAX(trade_date) AS day FROM {} WHERE feature_version=%s
                AND trade_date BETWEEN %s AND %s''').format(self._table('alpha158_daily')),
                (coverage['feature_version'], coverage['start_date'], min(str(as_of or coverage['end_date']), coverage['end_date']))).fetchone()['day']
            if chosen is None:
                raise ValueError('请求日期早于本人可用因子数据，不能生成股票池')
            where = sql.SQL('feature_version=%s AND trade_date=%s')
            params: list[Any] = [coverage['feature_version'], chosen]
            universe_count = conn.execute(sql.SQL('SELECT COUNT(*) AS n FROM {} WHERE {}').format(self._table('alpha158_daily'), where), params).fetchone()['n']
            if codes is not None:
                where += sql.SQL(' AND stock_code=ANY(%s)')
                params.append(codes)
            for item in filters:
                field, op = item['field'], item['op']
                if field not in FEATURE_COLUMNS or op not in (*operators, 'between'):
                    raise ValueError('股票池目前仅支持已存 Alpha158 因子白名单过滤')
                column = sql.Identifier(field)
                where += sql.SQL(" AND {} IS NOT NULL AND {} NOT IN ('NaN'::float8,'Infinity'::float8,'-Infinity'::float8)").format(column, column)
                if op == 'between':
                    where += sql.SQL(' AND {} BETWEEN %s AND %s').format(column)
                    params.extend((item['value'], item['upper']))
                else:
                    where += sql.SQL(' AND {} {} %s').format(column, sql.SQL(operators[op]))
                    params.append(item['value'])
            total = conn.execute(sql.SQL('SELECT COUNT(*) AS n FROM {} WHERE {}').format(self._table('alpha158_daily'), where), params).fetchone()['n']
            rows = conn.execute(sql.SQL('SELECT stock_code FROM {} WHERE {} ORDER BY stock_code LIMIT %s').format(self._table('alpha158_daily'), where), [*params, limit]).fetchall()
            selected = [row['stock_code'] for row in rows]
            return {'as_of': str(chosen), 'coverage': coverage, 'codes': selected, 'total': total,
                    'returned': len(selected), 'truncated': total > len(selected), 'universe_count': universe_count}
