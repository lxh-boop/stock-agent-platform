from __future__ import annotations

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.factor_catalog_service import FactorCatalogService
from core.user_scope import current_owner
from server.api.routers.web_factors import router
from server.api.security import AuthUser, require_user


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthUser('alice', 'Alice', 'user')
    return TestClient(app)


def test_factor_api_requires_authentication():
    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get('/api/v1/web/factors').status_code == 401


def test_factor_api_binds_identity_and_rejects_owner_override(client, monkeypatch):
    monkeypatch.setattr(FactorCatalogService, 'catalog', lambda self: {'owner': current_owner()})
    assert client.get('/api/v1/web/factors').json()['data'] == {'owner': 'alice'}
    body = {'name': '测试', 'expression': 'close', 'request_id': 'req-factor-x',
            'idempotency_key': 'idem-factor-x', 'confirmed': True, 'user_id': 'bob'}
    assert client.post('/api/v1/web/factors', json=body).status_code == 422
    assert not current_owner()


def test_factor_create_requires_confirmation_and_revalidates_expression(client, monkeypatch):
    calls = []
    monkeypatch.setattr('database.repositories.factor_catalog_repository.FactorCatalogRepository.create',
                        lambda self, *args, **kwargs: calls.append((args, kwargs)) or {'factor': {'name': args[0]}, 'replayed': False})
    body = {'name': '20 日均价比', 'expression': 'safe_div(mean(close, 20), close)',
            'request_id': 'req-factor-x', 'idempotency_key': 'idem-factor-x', 'confirmed': False}
    response = client.post('/api/v1/web/factors', json=body).json()
    assert not response['success'] and '确认' in response['error']['message'] and not calls
    body['confirmed'] = True
    body['expression'] = '__import__("os").getcwd()'
    assert not client.post('/api/v1/web/factors', json=body).json()['success']
    assert not calls
    body['expression'] = 'safe_div(mean(close, 20), close)'
    assert client.post('/api/v1/web/factors', json=body).json()['success']
    assert calls[0][1]['idempotency_key'] == 'idem-factor-x'


def test_factor_preview_groups_prices_by_stock_and_uses_only_past_rows():
    class Repository:
        def preview_prices(self, **kwargs):
            return pd.DataFrame({'code': ['000001', '000002', '000001', '000002'],
                                 'date': ['2026-11-17', '2026-11-17', '2026-11-18', '2026-11-18'],
                                 'close': [10., 200., 12., 100.]})

    result = FactorCatalogService(Repository()).validate('昨日价格', 'shift(close, 1)')
    values = {(row['code'], row['date']): row['value'] for row in result['preview']['items']}
    assert values['000001', '2026-11-18'] == 10
    assert values['000002', '2026-11-18'] == 200
    assert values['000001', '2026-11-17'] is None
    assert result['preview']['available_values'] == 2
    assert result['fields'] == ['close']


def test_factor_preview_has_honest_empty_history_status():
    class Repository:
        def preview_prices(self, **kwargs):
            return pd.DataFrame()

    result = FactorCatalogService(Repository()).validate('趋势', 'mean(close, 20)')
    assert result['valid'] is True
    assert result['preview']['status'] == 'empty'
    assert result['preview']['items'] == []


def test_factor_api_sanitizes_unexpected_errors_and_missing_records(client, monkeypatch):
    def failure(self):
        raise RuntimeError('postgresql://user:private-password@host/private-path')
    monkeypatch.setattr(FactorCatalogService, 'catalog', failure)
    response = client.get('/api/v1/web/factors')
    assert response.json()['error']['code'] == 'FACTOR_CATALOG_UNAVAILABLE'
    assert 'private-password' not in response.text and 'private-path' not in response.text
    def missing(self, *args, **kwargs):
        raise KeyError('private-factor')
    monkeypatch.setattr(FactorCatalogService, 'values', missing)
    response = client.get('/api/v1/web/factors/custom:missing/values')
    assert response.json()['error']['code'] == 'FACTOR_NOT_FOUND'
    assert client.get('/api/v1/web/factors/x/values?limit=1000000').status_code == 422
    assert client.get('/api/v1/web/factors/x/values?code=other-account').status_code == 422
