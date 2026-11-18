"""Account-owned factor definitions and bounded reads of stored factor values."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any
from uuid import uuid4

import pandas as pd

from core.alpha158_contract import FEATURE_COLUMNS, feature_version
from core.user_scope import current_owner, user_schema
from database.alpha158_store import Alpha158Store
from database.connection import transaction


class FactorCatalogRepository:
    def __init__(self) -> None:
        self.owner = current_owner()
        if not self.owner:
            raise ValueError('请先登录后查看因子库')

    def _check_owner(self) -> None:
        if current_owner() != self.owner:
            raise ValueError('因子库账号上下文不匹配')

    def _ensure(self, conn):
        from psycopg import sql
        from psycopg.types.json import Jsonb
        from core.factor_expressions import alpha158_definitions

        self._check_owner()
        schema = conn.execute('SELECT current_schema() AS name').fetchone()['name']
        if schema != user_schema(self.owner):
            raise ValueError('因子库必须使用当前账号的数据域')
        table = sql.Identifier(schema, 'factor_catalog')
        requests = sql.Identifier(schema, 'factor_catalog_requests')
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('factor_catalog:' + schema,))
        conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
            factor_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
            expression TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('alpha158', 'custom')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''').format(table))
        conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
            idempotency_key TEXT PRIMARY KEY, request_id TEXT NOT NULL,
            request_hash TEXT NOT NULL, factor_id TEXT NOT NULL REFERENCES {}(factor_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''').format(requests, table))
        seeds = [{**item, 'factor_id': 'alpha158:' + item['name'], 'name_key': item['name'].casefold()}
                 for item in alpha158_definitions()]
        if tuple(item['name'] for item in seeds) != FEATURE_COLUMNS or len(seeds) != 158:
            raise RuntimeError('alpha158_catalog_contract_mismatch')
        conn.execute(sql.SQL('''INSERT INTO {} (factor_id,name,name_key,expression,description,category,source)
            SELECT factor_id,name,name_key,expression,description,category,'alpha158'
            FROM jsonb_to_recordset(%s) AS seed(factor_id TEXT,name TEXT,name_key TEXT,
                expression TEXT,description TEXT,category TEXT)
            ON CONFLICT(factor_id) DO UPDATE SET expression=EXCLUDED.expression,
                description=EXCLUDED.description,category=EXCLUDED.category
            WHERE {}.source='alpha158' AND ({}.expression,{}.description,{}.category)
                IS DISTINCT FROM (EXCLUDED.expression,EXCLUDED.description,EXCLUDED.category)
            ''').format(table, table, table, table, table), (Jsonb(seeds),))
        return table, requests

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row[key] for key in ('factor_id', 'name', 'expression', 'description', 'category', 'source')} | {
            'created_at': row['created_at'].isoformat(), 'read_only': row['source'] == 'alpha158'}

    def list(self) -> list[dict[str, Any]]:
        from psycopg import sql
        with transaction() as conn:
            table, _ = self._ensure(conn)
            rows = conn.execute(sql.SQL('SELECT * FROM {} ORDER BY source, name_key').format(table)).fetchall()
            return [self._public(row) for row in rows]

    def get(self, factor_id: str) -> dict[str, Any]:
        from psycopg import sql
        with transaction() as conn:
            table, _ = self._ensure(conn)
            row = conn.execute(sql.SQL('SELECT * FROM {} WHERE factor_id=%s').format(table), (factor_id,)).fetchone()
            if row is None:
                raise KeyError('factor_not_found')
            return self._public(row)

    def create(self, name: str, expression: str, *, request_id: str, idempotency_key: str) -> dict[str, Any]:
        from psycopg import sql
        digest = hashlib.sha256(json.dumps({'name': name, 'expression': expression}, sort_keys=True,
                                          ensure_ascii=False).encode()).hexdigest()
        with transaction() as conn:
            table, requests = self._ensure(conn)
            previous = conn.execute(sql.SQL('SELECT * FROM {} WHERE idempotency_key=%s').format(requests),
                                    (idempotency_key,)).fetchone()
            if previous:
                if previous['request_hash'] != digest:
                    raise ValueError('该提交标识已用于其他因子，请重新确认后提交')
                row = conn.execute(sql.SQL('SELECT * FROM {} WHERE factor_id=%s').format(table),
                                   (previous['factor_id'],)).fetchone()
                return {'factor': self._public(row), 'replayed': True}
            if conn.execute(sql.SQL('SELECT 1 FROM {} WHERE name_key=%s').format(table), (name.casefold(),)).fetchone():
                raise ValueError('该因子名称已存在，请更换名称')
            count = conn.execute(sql.SQL("SELECT COUNT(*) AS n FROM {} WHERE source='custom'").format(table)).fetchone()['n']
            if count >= 200:
                raise ValueError('当前账号最多可保存 200 个自定义因子')
            factor_id = 'custom:' + uuid4().hex
            row = conn.execute(sql.SQL('''INSERT INTO {} (factor_id,name,name_key,expression,description,category,source)
                VALUES (%s,%s,%s,%s,'用户自定义因子','自定义','custom') RETURNING *''').format(table),
                (factor_id, name, name.casefold(), expression)).fetchone()
            conn.execute(sql.SQL('''INSERT INTO {} (idempotency_key,request_id,request_hash,factor_id)
                VALUES (%s,%s,%s,%s)''').format(requests), (idempotency_key, request_id, digest, factor_id))
            return {'factor': self._public(row), 'replayed': False}

    def stored_values(self, name: str, *, limit: int, code: str | None = None) -> dict[str, Any]:
        from psycopg import sql
        self._check_owner()
        if name not in FEATURE_COLUMNS:
            raise ValueError('不是可读取的 Alpha158 因子')
        with transaction() as conn:
            table, state = Alpha158Store._ensure(conn)
            meta = conn.execute(sql.SQL('SELECT * FROM {} WHERE feature_version=%s').format(state),
                                (feature_version(),)).fetchone()
            if not meta:
                return {'items': [], 'total': 0, 'available_values': 0, 'as_of': None}
            where = sql.SQL('feature_version=%s AND trade_date BETWEEN %s AND %s')
            params: list[Any] = [feature_version(), meta['retention_cutoff'], meta['latest_signal_date']]
            if code:
                where += sql.SQL(' AND stock_code=%s')
                params.append(code)
            column = sql.Identifier(name)
            counts = conn.execute(sql.SQL('''SELECT COUNT(*) AS total,
                COUNT(*) FILTER (WHERE {} IS NOT NULL AND {} NOT IN ('NaN'::float8,'Infinity'::float8,'-Infinity'::float8)) AS available_values
                FROM {} WHERE {}''').format(column, column, table, where), params).fetchone()
            rows = conn.execute(sql.SQL('''SELECT stock_code AS code,trade_date AS date,{} AS value
                FROM {} WHERE {} ORDER BY trade_date DESC,stock_code LIMIT %s''').format(column, table, where),
                [*params, limit]).fetchall()
            return {**counts, 'as_of': str(meta['latest_signal_date']), 'items': [
                {'code': row['code'], 'date': str(row['date']),
                 'value': float(row['value']) if row['value'] is not None and math.isfinite(row['value']) else None}
                for row in rows]}

    def preview_prices(self, *, rows_per_stock: int = 600, code: str | None = None) -> pd.DataFrame:
        """Read bounded raw inputs from the current account's persisted Alpha158 snapshot."""
        from psycopg import sql
        self._check_owner()
        rows_per_stock = min(600, max(1, int(rows_per_stock)))
        with transaction() as conn:
            table, state = Alpha158Store._ensure(conn)
            meta = conn.execute(sql.SQL('SELECT * FROM {} WHERE feature_version=%s').format(state),
                                (feature_version(),)).fetchone()
            if not meta:
                return pd.DataFrame()
            condition = sql.SQL('feature_version=%s AND trade_date BETWEEN %s AND %s')
            params: list[Any] = [feature_version(), meta['retention_cutoff'], meta['latest_signal_date']]
            if code:
                condition += sql.SQL(' AND stock_code=%s')
                params.append(code)
            codes = conn.execute(sql.SQL('''SELECT stock_code,MAX(trade_date) AS last_date FROM {}
                WHERE {} GROUP BY stock_code ORDER BY last_date DESC,stock_code LIMIT 3''').format(table, condition), params).fetchall()
            records = []
            for item in codes:
                rows = conn.execute(sql.SQL('''SELECT stock_code,trade_date,source_payload FROM {}
                    WHERE {} AND stock_code=%s ORDER BY trade_date DESC LIMIT %s''').format(table, condition),
                    [*params, item['stock_code'], rows_per_stock]).fetchall()
                records.extend({**row['source_payload'], 'code': row['stock_code'], 'date': row['trade_date']} for row in rows)
        if not records:
            return pd.DataFrame()
        frame = pd.DataFrame.from_records(records).sort_values(['code', 'date']).reset_index(drop=True)
        for key in ('open', 'high', 'low', 'close', 'volume', 'vwap', 'amount'):
            if key in frame:
                frame[key] = pd.to_numeric(frame[key], errors='coerce')
        return frame
