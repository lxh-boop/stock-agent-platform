"""Alpha158 的 PostgreSQL 权威存储；沿用当前用户 schema，不回落公共表。"""
from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Iterator

import pandas as pd
from core.alpha158_contract import FEATURE_COLUMNS, CONTEXT_COLUMNS, as_date, retention_cutoff, feature_version
from core.user_scope import current_owner, user_schema
from database.connection import transaction


def _json_number(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    return value.item() if hasattr(value, 'item') else value


class Alpha158Store:
    """每个实例绑定创建时的身份，避免请求间复用导致跨用户读写。"""
    def __init__(self) -> None:
        self.owner = current_owner()
        if not self.owner:
            raise ValueError("alpha158_authenticated_user_required")

    def _check_owner(self) -> None:
        if current_owner() != self.owner:
            raise ValueError('alpha158_store_owner_mismatch')

    @staticmethod
    def _ensure(conn):
        from psycopg import sql
        schema = conn.execute('SELECT current_schema() AS name').fetchone()['name']
        if schema != user_schema(current_owner()):
            raise ValueError('alpha158_user_schema_required')
        # 使用完全限定表名，不能因 search_path 缺表而借用公共数据。
        table = sql.Identifier(schema, 'alpha158_daily')
        state = sql.Identifier(schema, 'alpha158_state')
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('alpha158:' + schema,))
        definitions = sql.SQL(', ').join(sql.SQL('{} DOUBLE PRECISION').format(sql.Identifier(c)) for c in FEATURE_COLUMNS)
        conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
            feature_version TEXT NOT NULL, trade_date DATE NOT NULL, stock_code VARCHAR(32) NOT NULL,
            input_hash TEXT NOT NULL, source_payload JSONB NOT NULL,
            computed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            {}, PRIMARY KEY (feature_version, stock_code, trade_date))''').format(table, definitions))
        conn.execute(sql.SQL('CREATE INDEX IF NOT EXISTS alpha158_daily_date_idx ON {} (trade_date)').format(table))
        conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
            feature_version TEXT PRIMARY KEY, latest_signal_date DATE NOT NULL,
            retention_cutoff DATE NOT NULL, feature_names JSONB NOT NULL,
            rows_written BIGINT NOT NULL DEFAULT 0, rows_deleted BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''').format(state))
        return table, state

    @contextmanager
    def update(self, as_of: object) -> Iterator['_Writer']:
        self._check_owner()
        with transaction() as conn:
            table, state = self._ensure(conn)
            writer = _Writer(conn, table, state, as_date(as_of))
            yield writer  # 异常时由 transaction 回滚全部写入及清理。

    def read(self, *, recent_trading_days: int | None = 120, end_date: object | None = None,
             stock_codes: list[str] | None = None) -> pd.DataFrame:
        self._check_owner()
        from psycopg import sql
        if recent_trading_days is not None and int(recent_trading_days) <= 0:
            raise ValueError('alpha158_recent_days_must_be_positive')
        with transaction() as conn:
            table, state = self._ensure(conn)
            meta = conn.execute(sql.SQL('SELECT * FROM {} WHERE feature_version=%s').format(state), (feature_version(),)).fetchone()
            if not meta:
                return pd.DataFrame(columns=['date', 'code', *CONTEXT_COLUMNS, *FEATURE_COLUMNS])
            end = min(as_date(end_date), meta['latest_signal_date']) if end_date is not None else meta['latest_signal_date']
            lower = meta['retention_cutoff']
            if recent_trading_days is not None:
                dates = conn.execute(sql.SQL('''SELECT DISTINCT trade_date FROM {}
                    WHERE feature_version=%s AND trade_date BETWEEN %s AND %s
                    ORDER BY trade_date DESC LIMIT %s''').format(table),
                    (feature_version(), lower, end, int(recent_trading_days))).fetchall()
                if dates:
                    lower = min(row['trade_date'] for row in dates)
            condition = sql.SQL('feature_version=%s AND trade_date BETWEEN %s AND %s')
            params: list[Any] = [feature_version(), lower, end]
            if stock_codes is not None:
                condition += sql.SQL(' AND stock_code=ANY(%s)')
                params.append([str(code).zfill(6) for code in stock_codes])
            chunks = []
            # 服务端游标分批取数；不先把整个五年宽表取到客户端游标内存。
            with conn.cursor(name='alpha158_read') as cursor:
                cursor.execute(sql.SQL('SELECT * FROM {} WHERE {} ORDER BY stock_code, trade_date').format(table, condition), params)
                while rows := cursor.fetchmany(1000):
                    chunks.append(pd.DataFrame.from_records([
                        {**row['source_payload'], 'date': row['trade_date'], 'code': row['stock_code'],
                         **{key: row[key] for key in FEATURE_COLUMNS}} for row in rows]))
        if not chunks:
            return pd.DataFrame(columns=['date', 'code', *CONTEXT_COLUMNS, *FEATURE_COLUMNS])
        result = pd.concat(chunks, ignore_index=True)
        result['date'] = pd.to_datetime(result['date'])
        for col in (*FEATURE_COLUMNS, *(c for c in CONTEXT_COLUMNS if c != 'name')):
            if col in result:
                result[col] = pd.to_numeric(result[col], errors='coerce').astype('float64')
        return result


class _Writer:
    def __init__(self, conn, table, state, as_of) -> None:
        from psycopg import sql
        self.conn, self.table, self.state, self.as_of = conn, table, state, as_of
        self.cutoff, self.version, self.written = retention_cutoff(as_of), feature_version(), 0
        previous = conn.execute(sql.SQL('SELECT MAX(latest_signal_date) AS d FROM {}').format(state)).fetchone()['d']
        if previous is not None and as_of < previous:
            raise ValueError('alpha158_stale_signal_date')
        conn.execute(sql.SQL('CREATE TEMP TABLE alpha158_stage (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP').format(table))

    def fingerprints(self, code: str) -> dict:
        from psycopg import sql
        rows = self.conn.execute(sql.SQL('''SELECT trade_date, input_hash FROM {}
            WHERE feature_version=%s AND stock_code=%s AND trade_date BETWEEN %s AND %s''').format(self.table),
            (self.version, code, self.cutoff, self.as_of)).fetchall()
        return {row['trade_date']: row['input_hash'] for row in rows}

    def stage(self, features: pd.DataFrame, hashes: dict) -> None:
        from psycopg import sql
        columns = ('feature_version', 'trade_date', 'stock_code', 'input_hash', 'source_payload', *FEATURE_COLUMNS)
        statement = sql.SQL('COPY alpha158_stage ({}) FROM STDIN').format(sql.SQL(', ').join(map(sql.Identifier, columns)))
        with self.conn.cursor().copy(statement) as copy:
            for row in features.to_dict(orient='records'):
                day, code = as_date(row['date']), str(row['code'])
                if not self.cutoff <= day <= self.as_of:
                    raise ValueError('alpha158_row_outside_retention_window')
                # 不保存 future_* 标签；只有已知当日行情和回看指标。
                payload = {key: _json_number(row[key]) for key in CONTEXT_COLUMNS if key in row}
                values = tuple(_json_number(row[key]) for key in FEATURE_COLUMNS)
                copy.write_row((self.version, day, code, hashes[day], json.dumps(payload, ensure_ascii=False, allow_nan=False), *values))
                self.written += 1

    def finish(self) -> dict[str, Any]:
        from psycopg import sql
        columns = ('feature_version', 'trade_date', 'stock_code', 'input_hash', 'source_payload', 'computed_at', *FEATURE_COLUMNS)
        names = sql.SQL(', ').join(map(sql.Identifier, columns))
        changes = sql.SQL(', ').join(sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c), sql.Identifier(c))
                                      for c in columns if c not in ('feature_version', 'trade_date', 'stock_code'))
        self.conn.execute(sql.SQL('''INSERT INTO {} ({}) SELECT {} FROM alpha158_stage WHERE true
            ON CONFLICT (feature_version, stock_code, trade_date) DO UPDATE SET {}''').format(self.table, names, names, changes))
        present = self.conn.execute(sql.SQL('SELECT COUNT(*) AS n FROM {} WHERE feature_version=%s AND trade_date=%s').format(self.table),
                                    (self.version, self.as_of)).fetchone()['n']
        if not present:
            raise RuntimeError('alpha158_target_date_missing_no_prune')
        deleted = self.conn.execute(sql.SQL('DELETE FROM {} WHERE trade_date < %s').format(self.table), (self.cutoff,)).rowcount
        self.conn.execute(sql.SQL('''INSERT INTO {} (feature_version,latest_signal_date,retention_cutoff,feature_names,rows_written,rows_deleted)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (feature_version) DO UPDATE SET
            latest_signal_date=EXCLUDED.latest_signal_date, retention_cutoff=EXCLUDED.retention_cutoff,
            feature_names=EXCLUDED.feature_names, rows_written=EXCLUDED.rows_written,
            rows_deleted=EXCLUDED.rows_deleted, updated_at=CURRENT_TIMESTAMP''').format(self.state),
            (self.version, self.as_of, self.cutoff, json.dumps(FEATURE_COLUMNS), self.written, deleted))
        return {'feature_version': self.version, 'signal_date': str(self.as_of), 'retention_cutoff': str(self.cutoff),
                'rows_written': self.written, 'rows_deleted': deleted, 'storage': 'postgresql/alpha158_daily'}
