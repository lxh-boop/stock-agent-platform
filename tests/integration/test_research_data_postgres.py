"""Real account SQL, historical custom factors and truthful point-in-time pools."""
from __future__ import annotations

import os
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from application.factor_catalog_service import FactorCatalogService
from application.research_data_service import ResearchDataService
from core.user_scope import user_scope
from database.connection import get_connection
from database.repositories.research_data_repository import ResearchDataRepository
from factor_storage import refresh_alpha158

pytestmark = pytest.mark.skipif(os.getenv('STOCK_TEST_DATABASE') != '1', reason='isolated test database only')


@pytest.fixture(scope='module', autouse=True)
def schema(isolated_postgres_schema):
    yield


@pytest.fixture
def owner():
    return 'research_' + uuid4().hex[:16]


def populate(owner):
    frames = []
    for index, code in enumerate(('000001', '000002', '600000')):
        values = np.arange(4, dtype=float) + 10 + index * 100
        frames.append(pd.DataFrame({'date': pd.bdate_range('2026-01-05', periods=4), 'code': code,
                                    'open': values - 1, 'high': values + 1, 'low': values - 2,
                                    'close': values, 'volume': 1000., 'vwap': values}))
    raw = pd.concat(frames, ignore_index=True)
    with user_scope(owner):
        refresh_alpha158(raw, as_of='2026-01-08')
        factor = FactorCatalogService().create(name='昨日收盘', expression='shift(close, 1)',
                    request_id='req-test', idempotency_key='idem-test', confirmed=True)['factor']
        with get_connection() as conn:
            conn.execute('''UPDATE alpha158_daily SET source_payload=source_payload || '{"provider_token":"test-never-return"}'::jsonb''')
            conn.execute("INSERT INTO stock_basic(stock_code,stock_name,industry) VALUES ('000001','本人证券','当前行业')")
            conn.execute("INSERT INTO market_data_daily(trade_date,stock_code,close) VALUES ('20260107','000001',123)")
            conn.commit()
    return factor


def test_real_catalog_and_reads_only_whitelisted_personal_columns(owner):
    populate(owner)
    with user_scope(owner):
        service = ResearchDataService()
        tables = {item['table']: item for item in service.list_factor_tables()['tables']}
        assert tables['alpha158_daily']['coverage']['row_count'] == 12
        assert tables['alpha158_daily']['coverage']['stock_count'] == 3
        assert tables['custom_factors']['coverage']['definition_count'] == 1
        price = service.read_factor_data('market_data_daily', ['close'], '2026-01-07', '000001.XSHE')
        assert price['data'] == [{'code': '000001', 'date': '2026-01-07', 'close': 123.}]
        master = service.read_factor_data('stock_basic', ['stock_name','industry'], '20200101', '000001')
        assert master['data'][0]['industry'] == '当前行业' and '当前信息' in master['message']
        factor = service.read_factor_data('alpha158_daily', ['KMID'], '20260107', '000001')
        assert factor['data'][0]['KMID'] is not None
        assert 'test-never-return' not in str(factor) and 'source_payload' not in str(factor)


def test_custom_historical_lookup_has_no_future_fill_and_no_cross_user_access(owner):
    factor = populate(owner)
    with user_scope(owner):
        service = ResearchDataService()
        result = service.read_factor_data('custom_factors', [factor['factor_id']], '20260107', '000001')
        assert result['data'][0][factor['factor_id']] == 11.
        assert 'test-never-return' not in str(result)
        assert service.read_factor_data('custom_factors', [factor['factor_id']], '20260109', '000001')['data'] == []
        assert service.read_factor_data('alpha158_daily', ['KMID'], '20260109', '000001')['data'] == []
        repo = ResearchDataRepository()
    with user_scope('research_' + uuid4().hex[:16]):
        with pytest.raises(ValueError, match='账号上下文'):
            repo.catalog()
        other = ResearchDataService()
        assert other.read_factor_data('alpha158_daily', ['KMID'], '20260107', '000001')['data'] == []
        with pytest.raises(ValueError, match='不属于'):
            other.read_factor_data('custom_factors', [factor['factor_id']], '20260107', '000001')


def test_pool_queries_actual_as_of_data_and_reports_truncation(owner):
    populate(owner)
    with user_scope(owner):
        service = ResearchDataService()
        pool = service.screen_stock_pool(as_of='20260110', limit=2)
        assert pool['as_of'] == '2026-01-08'
        assert pool['total'] == 3 and pool['returned'] == 2 and pool['truncated'] is True
        assert pool['codes'] == ['000001', '000002']
        filtered = service.screen_stock_pool(as_of='20260107', filters=[{'field':'KMID','op':'gt','value':.05}])
        assert filtered['codes'] == ['000001'] and filtered['as_of'] == '2026-01-07'
        disjoint = service.screen_stock_pool(groups=[{'kind':'list','ids':['000001']}, {'kind':'list','ids':['600000']}])
        assert disjoint['codes'] == [] and disjoint['total'] == 0
        with pytest.raises(ValueError, match='早于'):
            service.screen_stock_pool(as_of='20200101')
        # Querying did not create snapshots, pool handles, or business records.
        with get_connection() as conn:
            assert conn.execute('SELECT COUNT(*) AS n FROM alpha158_daily').fetchone()['n'] == 12
            assert conn.execute('SELECT COUNT(*) AS n FROM factor_catalog_requests').fetchone()['n'] == 1
