"""仅临时测试数据库：真实 COPY/upsert/回滚/五年清理和用户域。"""
from __future__ import annotations
import os
from datetime import date
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from core.user_scope import user_scope, user_schema
from core.alpha158_contract import FEATURE_COLUMNS, feature_version
from database.alpha158_store import Alpha158Store, _Writer
from database.connection import get_connection
from factor_storage import refresh_alpha158, load_stored_alpha158

pytestmark = pytest.mark.skipif(os.environ.get('STOCK_TEST_DATABASE') != '1', reason='isolated test database only')
USERS = ('factor_alice', 'factor_bob')


@pytest.fixture(scope='module', autouse=True)
def schema():
    psycopg = pytest.importorskip('psycopg')
    from database.postgres_config import PostgresSettings
    settings = PostgresSettings.from_env()
    assert settings.database == 'stock_test_isolation', 'Refuse non-test database'
    script = (Path(__file__).resolve().parents[2] / 'database/postgres_migrations/001_baseline.sql').read_text()
    script = '\n'.join(line for line in script.splitlines() if not line.startswith('\\'))
    with psycopg.connect(**settings.connection_kwargs()) as conn:
        conn.execute(script)
    yield


@pytest.fixture(autouse=True)
def reset_factors(schema):
    from psycopg import sql
    for user in USERS:
        with user_scope(user):
            store = Alpha158Store()
            store.read()
            with get_connection() as conn:
                conn.execute(sql.SQL('TRUNCATE {}.alpha158_daily, {}.alpha158_state').format(
                    sql.Identifier(user_schema(user)), sql.Identifier(user_schema(user))))
                conn.commit()


def sample(days=('2021-11-12','2021-11-13','2021-11-14')):
    close = np.arange(len(days), dtype=float) + 10
    return pd.DataFrame({'date': pd.to_datetime(days), 'code':'000001','name':'test',
                         'open':close-.1,'high':close+.2,'low':close-.2,'close':close,
                         'volume':1000.,'vwap':close})


def test_copy_roundtrip_exact_158_columns_no_future_labels():
    raw = sample()
    with user_scope(USERS[0]):
        stats = refresh_alpha158(raw, as_of=raw.date.max())
        assert stats['rows_written'] == 3
        result = load_stored_alpha158()
        assert len(result) == 3
        assert all(name in result for name in FEATURE_COLUMNS)
        with get_connection() as conn:
            record = conn.execute('SELECT * FROM alpha158_daily ORDER BY trade_date LIMIT 1').fetchone()
            assert sum(name in record for name in FEATURE_COLUMNS) == 158
            assert not any(name.startswith('future_') for name in record['source_payload'])
            assert record['MA60'] is None
        assert refresh_alpha158(raw, as_of=raw.date.max())['rows_written'] == 0


def test_retention_inclusive_boundary_and_own_user_only():
    raw = sample()
    for user in USERS:
        with user_scope(user):
            refresh_alpha158(raw, as_of=raw.date.max())
    new = pd.concat([raw, sample(('2026-11-13',))],ignore_index=True)
    with user_scope(USERS[0]):
        stats = refresh_alpha158(new, as_of='2026-11-13')
        assert stats['rows_deleted'] == 1
        result = load_stored_alpha158(recent_trading_days=None)
        assert result.date.min().date() == date(2021,11,13)
        assert result.date.max().date() == date(2026,11,13)
    with user_scope(USERS[1]):
        assert load_stored_alpha158().date.min().date() == date(2021,11,12)


def test_failure_after_prune_rolls_back_data_and_watermark(monkeypatch):
    raw = sample()
    with user_scope(USERS[0]):
        refresh_alpha158(raw, as_of=raw.date.max())
        original = _Writer.finish
        def fail(self):
            original(self)
            raise RuntimeError('injected_after_delete')
        monkeypatch.setattr(_Writer, 'finish', fail)
        new = pd.concat([raw, sample(('2026-11-13',))],ignore_index=True)
        with pytest.raises(RuntimeError, match='injected_after_delete'):
            refresh_alpha158(new, as_of='2026-11-13')
        result = load_stored_alpha158()
        assert len(result) == 3 and result.date.min().date() == date(2021,11,12)
        with get_connection() as conn:
            meta = conn.execute('SELECT latest_signal_date FROM alpha158_state').fetchone()
            assert meta['latest_signal_date'] == date(2021,11,14)


def test_same_code_date_different_users_and_no_scope_fallback():
    raw = sample()
    with pytest.raises(ValueError, match='authenticated_user'):
        Alpha158Store()
    with user_scope(USERS[0]):
        alice = Alpha158Store()
        refresh_alpha158(raw, as_of=raw.date.max(), store=alice)
    with user_scope(USERS[1]):
        assert Alpha158Store().read().empty
        changed = raw.copy()
        changed['open'] = changed.close * .5
        refresh_alpha158(changed, as_of=changed.date.max())
        bob = load_stored_alpha158()['KMID'].iloc[0]
        with pytest.raises(ValueError, match='owner_mismatch'):
            alice.read()
    with user_scope(USERS[0]):
        assert load_stored_alpha158()['KMID'].iloc[0] != bob


def test_stale_signal_and_recent_query():
    raw = sample()
    with user_scope(USERS[0]):
        refresh_alpha158(raw, as_of=raw.date.max())
        assert len(load_stored_alpha158(recent_trading_days=2)) == 2
        with pytest.raises(ValueError, match='stale_signal'):
            refresh_alpha158(raw, as_of='2021-11-13')
        assert len(load_stored_alpha158()) == 3
