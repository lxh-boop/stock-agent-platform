"""Real PostgreSQL: seeding, stored values, private expressions, replay and isolation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from application.factor_catalog_service import FactorCatalogService
from core.alpha158_contract import FEATURE_COLUMNS, feature_version
from core.user_scope import user_scope
from database.connection import get_connection
from database.repositories.factor_catalog_repository import FactorCatalogRepository
from factor_storage import refresh_alpha158

pytestmark = pytest.mark.skipif(os.getenv('STOCK_TEST_DATABASE') != '1', reason='isolated test database only')


@pytest.fixture(scope='module', autouse=True)
def schema(isolated_postgres_schema):
    yield


@pytest.fixture
def owner():
    # Independent account schemas keep retries/tests from overwriting each other.
    return 'fc_' + uuid4().hex[:16]


def create(service, name='价格趋势', expression='mean(close, 5)', key='first'):
    return service.create(name=name, expression=expression, request_id='req-' + key,
                          idempotency_key='idem-' + key, confirmed=True)


def prices(days=80, codes=('000001', '000002')):
    chunks = []
    for index, code in enumerate(codes):
        close = np.arange(days, dtype=float) + 10 + index * 100
        chunks.append(pd.DataFrame({'code': code, 'date': pd.bdate_range('2026-01-01', periods=days),
                                    'open': close - .2, 'high': close + .5, 'low': close - .5,
                                    'close': close, 'volume': 1000., 'vwap': close}))
    return pd.concat(chunks, ignore_index=True)


def test_catalog_seeds_exactly_158_and_reads_persisted_definitions(owner):
    with user_scope(owner):
        repo = FactorCatalogRepository()
        first = repo.list()
        assert len(first) == 158
        assert {row['name'] for row in first} == set(FEATURE_COLUMNS)
        assert all(row['source'] == 'alpha158' and row['read_only'] for row in first)
        assert len(repo.list()) == 158
        saved = create(FactorCatalogService(repo))['factor']
        # This is an actual database definition, not an in-memory list merged into the page.
        with get_connection() as conn:
            assert conn.execute('SELECT COUNT(*) AS n FROM factor_catalog').fetchone()['n'] == 159
            conn.execute('UPDATE factor_catalog SET description=%s WHERE factor_id=%s', ('数据库中的说明', saved['factor_id']))
            conn.commit()
        assert repo.get(saved['factor_id'])['description'] == '数据库中的说明'


def test_custom_definitions_and_values_never_cross_accounts(owner):
    with user_scope(owner):
        repo = FactorCatalogRepository()
        own = create(FactorCatalogService(repo))['factor']
    with user_scope('fc_' + uuid4().hex[:16]):
        other = FactorCatalogRepository()
        assert len(other.list()) == 158
        with pytest.raises(KeyError):
            other.get(own['factor_id'])
        with pytest.raises(ValueError, match='账号上下文'):
            repo.get(own['factor_id'])
        same_name = create(FactorCatalogService(other), expression='mean(close, 10)')['factor']
        assert same_name['factor_id'] != own['factor_id']
    with pytest.raises(ValueError, match='登录'):
        FactorCatalogRepository()


def test_replay_is_atomic_and_rejects_changed_payload_and_reserved_names(owner):
    def submit():
        with user_scope(owner):
            return create(FactorCatalogService())
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert {result['replayed'] for result in results} == {False, True}
    assert len({result['factor']['factor_id'] for result in results}) == 1
    with user_scope(owner):
        service = FactorCatalogService()
        assert service.catalog()['custom_count'] == 1
        with pytest.raises(ValueError, match='提交标识'):
            create(service, expression='mean(close, 10)')
        with pytest.raises(ValueError, match='名称已存在'):
            create(service, name='kmid', key='reserved')


def test_alpha_values_are_read_from_database_and_preview_uses_own_raw_prices(owner):
    raw = prices()
    with user_scope(owner):
        refresh_alpha158(raw, as_of=raw.date.max())
        service = FactorCatalogService()
        with get_connection() as conn:
            conn.execute('UPDATE alpha158_daily SET "KMID"=777 WHERE stock_code=%s AND trade_date=%s AND feature_version=%s',
                         ('000001', raw.date.max().date(), feature_version()))
            conn.commit()
        result = service.values('alpha158:KMID', code='000001', limit=1)
        assert result['items'][0]['value'] == 777
        assert result['total'] == 80 and result['available_values'] == 80
        assert result['source'] == 'stored'
        saved = create(service, expression='shift(close, 1)')['factor']
        preview = service.values(saved['factor_id'], limit=2)
        assert preview['source'] == 'computed'
        expected = raw.sort_values('date').groupby('code')['close'].nth(-2).tolist()
        assert sorted(row['value'] for row in preview['items']) == sorted(expected)
        assert preview['total'] == 160 and preview['available_values'] == 158
        with get_connection() as conn:
            assert conn.execute('SELECT COUNT(*) AS n FROM alpha158_daily').fetchone()['n'] == 160


def test_empty_and_insufficient_history_are_not_fake_successful_values(owner):
    with user_scope(owner):
        service = FactorCatalogService()
        result = service.values('alpha158:MA60')
        assert result['status'] == 'empty' and result['total'] == 0
        raw = prices(days=3, codes=('000001',))
        refresh_alpha158(raw, as_of=raw.date.max())
        result = service.values('alpha158:MA60')
        assert result['status'] == 'insufficient_history' and result['available_values'] == 0
        preview = service.validate('长周期均线', 'mean(close, 60)')['preview']
        assert preview['status'] == 'insufficient_history'
        assert all(row['value'] is None for row in preview['items'])
