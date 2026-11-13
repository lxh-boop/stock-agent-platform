"""个人指数缓存：仅使用本人的 Tushare；不借用平台 Token 或虚构基准。"""
from datetime import timedelta
import hashlib
import json

import pandas as pd
from backtesting.contracts import INDEX_CODES, INDEX_FIELDS
from backtesting.signals import finite
from core.user_scope import current_owner, user_schema
from database.connection import transaction


def _table(conn):
    from psycopg import sql
    owner = current_owner()
    if not owner: raise ValueError('个人指数数据需要登录')
    schema = user_schema(owner)
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))', ('backtest-market:' + schema,))
    table = sql.Identifier(schema, 'backtest_index_cache')
    conn.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS {} (
        index_key TEXT PRIMARY KEY, start_date DATE NOT NULL, end_date DATE NOT NULL,
        data JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''').format(table))
    return table


def normalize_index(frame, calendar: list[str], start, end) -> list[dict]:
    if frame is None or frame.empty: raise ValueError('指数接口没有返回数据')
    required = {'trade_date', 'open', 'high', 'low', 'close', 'pre_close'}
    if not required <= set(frame.columns): raise ValueError('指数行情字段不完整')
    data = frame.copy()
    data['date'] = pd.to_datetime(data['trade_date'], format='%Y%m%d', errors='coerce')
    if data['date'].isna().any() or data['date'].duplicated().any(): raise ValueError('指数交易日重复或无效')
    data = data.sort_values('date')
    data = data.loc[(data['date'].dt.date >= start) & (data['date'].dt.date <= end)].copy()
    actual = set(data['date'].dt.strftime('%Y%m%d'))
    if not set(calendar) <= actual: raise ValueError('指数行情缺少交易日，不能填零生成基准')
    for field in required - {'trade_date'}:
        data[field] = pd.to_numeric(data[field], errors='coerce')
        if data[field].isna().any() or (data[field] <= 0).any(): raise ValueError('指数价格无效')
    data['volume'] = pd.to_numeric(data.get('vol', pd.Series(index=data.index, dtype=float)), errors='coerce')
    data['pct_chg'] = (data['close'] / data['pre_close'] - 1) * 100
    for n in (5, 20, 60): data[f'ma{n}'] = data['close'].rolling(n, min_periods=n).mean()
    data['close_ma20'] = data['close'] / data['ma20']
    for n in (5, 20): data[f'ret{n}'] = data['close'] / data['close'].shift(n) - 1
    data['vol20'] = data['close'].pct_change(fill_method=None).rolling(20, min_periods=20).std()
    data['drawdown20'] = data['close'] / data['close'].rolling(20, min_periods=20).max() - 1
    return [{'date': str(row['date'].date()), **{f: finite(row.get(f)) for f in INDEX_FIELDS}}
            for row in data.to_dict(orient='records')]


def prepare(spec, *, cancelled=lambda: False, client=None) -> None:
    needed = spec.indexes()
    if not needed: return
    # 60日均线需要预热；动态IC自身的预热从因子库读取。
    start, end = spec.start_date - timedelta(days=400), spec.end_date
    from psycopg import sql
    for key in sorted(needed):
        if cancelled(): raise InterruptedError('回测已取消')
        with transaction() as conn:
            table = _table(conn)
            cached = conn.execute(sql.SQL('SELECT * FROM {} WHERE index_key=%s').format(table), (key,)).fetchone()
        if cached and cached['start_date'] <= start and cached['end_date'] >= end:
            continue
        # 每次需要扩展时重取完整请求区间，不以缓存最早/最晚日期冒充中间无缺口。
        if client is None:
            from application.user_credentials_service import get_user_tushare_token
            from data_tushare import init_tushare_pro
            client = init_tushare_pro(get_user_tushare_token(current_owner()))
        try:
            calendar = client.trade_cal(exchange='SSE', start_date=start.strftime('%Y%m%d'),
                end_date=end.strftime('%Y%m%d'), is_open='1', fields='cal_date,is_open')
            if calendar is None or calendar.empty: raise ValueError('交易日历为空')
            dates = sorted(calendar.loc[calendar['is_open'].astype(str) == '1', 'cal_date'].astype(str).tolist())
            frames, cursor = [], start
            while cursor <= end:
                if cancelled(): raise InterruptedError('回测已取消')
                last = min(end, cursor + timedelta(days=365))
                frames.append(client.index_daily(ts_code=INDEX_CODES[key], start_date=cursor.strftime('%Y%m%d'),
                    end_date=last.strftime('%Y%m%d'), fields='trade_date,open,high,low,close,pre_close,vol,amount'))
                cursor = last + timedelta(days=1)
            data = normalize_index(pd.concat(frames, ignore_index=True), dates, start, end)
        except InterruptedError: raise
        except Exception:
            raise ValueError(f'{key} 指数或日历下载不完整，请检查本人 Tushare 权限、额度和网络；未生成替代基准') from None
        if cancelled(): raise InterruptedError('回测已取消')
        from psycopg.types.json import Jsonb
        with transaction() as conn:
            table = _table(conn)
            conn.execute(sql.SQL('''INSERT INTO {} (index_key,start_date,end_date,data) VALUES (%s,%s,%s,%s)
                ON CONFLICT(index_key) DO UPDATE SET start_date=EXCLUDED.start_date,end_date=EXCLUDED.end_date,
                data=EXCLUDED.data,updated_at=CURRENT_TIMESTAMP''').format(table), (key, start, end, Jsonb(data)))


def read_snapshot(conn, spec) -> tuple[dict, dict]:
    from psycopg import sql
    if not spec.indexes(): return {}, {}
    table = sql.Identifier(user_schema(current_owner()), 'backtest_index_cache')
    market, source = {}, {}
    for key in sorted(spec.indexes()):
        row = conn.execute(sql.SQL('SELECT * FROM {} WHERE index_key=%s').format(table), (key,)).fetchone()
        if not row or row['end_date'] < spec.end_date or row['start_date'] > spec.start_date:
            raise ValueError('个人指数缓存覆盖不足，请重新运行回测')
        for value in row['data']:
            day = pd.Timestamp(value['date']).date()
            market.setdefault(day, {})[key] = {f: value.get(f) for f in INDEX_FIELDS}
        source[key] = {'updated_at': row['updated_at'].isoformat(), 'source': 'personal_tushare/index_daily',
                       'hash': hashlib.sha256(json.dumps(row['data'], sort_keys=True).encode()).hexdigest()}
    return market, source
