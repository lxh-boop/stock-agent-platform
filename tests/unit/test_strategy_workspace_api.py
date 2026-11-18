"""Strategy workspace HTTP contracts bind identity and reviewed versions."""
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from application.strategy_service import StrategyService
from core.user_scope import current_owner
from server.api.routers.web_strategies import router
from server.api.security import AuthUser, require_user


def params():
    return {'name': 'Trend', 'start_date': '2026-01-05', 'end_date': '2026-01-08', 'factors': [{'field': 'KMID'}]}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthUser('alice', 'Alice', 'user')
    return TestClient(app)


def test_production_router_registered_and_anonymous_requests_rejected():
    from server.api.main import create_app
    client = TestClient(create_app())
    assert client.get('/api/v1/web/strategies').status_code == 401
    assert client.get('/api/v1/web/strategies/' + str(uuid4())).status_code == 401


def test_list_uses_authenticated_owner_and_validates_pagination(client, monkeypatch):
    calls = []
    monkeypatch.setattr(StrategyService, 'list', lambda limit, offset: calls.append((current_owner(), limit, offset)) or {'records': [], 'total': 0})
    assert client.get('/api/v1/web/strategies?limit=12&offset=24').status_code == 200
    assert calls == [('alice', 12, 24)] and not current_owner()
    assert client.get('/api/v1/web/strategies?limit=10000').status_code == 422
    assert client.get('/api/v1/web/strategies?offset=-1').status_code == 422


def test_from_report_requires_confirmation_and_does_not_accept_owner(client, monkeypatch):
    calls = []
    monkeypatch.setattr(StrategyService, 'from_report', lambda run_id, **kw: calls.append((current_owner(), run_id, kw)) or {'strategy_id': kw['request_id']})
    body = {'run_id': str(uuid4()), 'request_id': str(uuid4()), 'name': 'Saved', 'confirmed': True}
    assert client.post('/api/v1/web/strategies/from-report', json=body).json()['data']['strategy_id'] == body['request_id']
    assert calls[0][0] == 'alice' and calls[0][2]['name'] == 'Saved'
    assert client.post('/api/v1/web/strategies/from-report', json={**body, 'confirmed': False}).status_code == 422
    assert client.post('/api/v1/web/strategies/from-report', json={**body, 'owner_id': 'bob'}).status_code == 422
    assert client.post('/api/v1/web/strategies/from-report', json={**body, 'name': 'x' * 81}).status_code == 422
    assert len(calls) == 1


def test_save_and_rerun_forward_reviewed_version_and_sanitize_task(client, monkeypatch):
    calls = []
    ident = str(uuid4())
    monkeypatch.setattr(StrategyService, 'save', lambda strategy_id, spec, **kw: calls.append((current_owner(), strategy_id, kw)) or {'version': 2})
    body = {'spec': params(), 'request_id': str(uuid4()), 'expected_version': 1, 'confirmed': True}
    assert client.put('/api/v1/web/strategies/' + ident, json=body).json()['data']['version'] == 2
    assert calls[0][2]['expected_version'] == 1
    monkeypatch.setattr(StrategyService, 'rerun', lambda strategy_id, **kw: {'task_id': 'task_test', 'status': 'queued', 'request': {'kwargs': {'api_key': 'secret', 'spec': params()}}})
    request = {'request_id': str(uuid4()), 'expected_version': 2, 'confirmed': True}
    response = client.post(f'/api/v1/web/strategies/{ident}/rerun', json=request)
    assert response.json()['data']['task_id'] == 'task_test' and 'secret' not in response.text
    assert client.post(f'/api/v1/web/strategies/{ident}/rerun', json={**request, 'expected_version': None}).status_code == 422


def test_delete_binds_version_and_uuid_request(client, monkeypatch):
    calls = []
    monkeypatch.setattr(StrategyService, 'delete', lambda strategy_id, **kw: calls.append((current_owner(), strategy_id, kw)) or {'deleted': True})
    path = '/api/v1/web/strategies/' + str(uuid4())
    request = {'confirmed': True, 'request_id': str(uuid4()), 'expected_version': 3}
    assert client.request('DELETE', path, json=request).json()['data']['deleted']
    assert calls[0][0] == 'alice' and calls[0][2]['expected_version'] == 3
    assert client.request('DELETE', path, json={**request, 'confirmed': False}).status_code == 422
    assert client.request('DELETE', path, json={'confirmed': True, 'request_id': request['request_id']}).status_code == 422


def test_missing_conflict_and_internal_errors_have_safe_responses(client, monkeypatch):
    def missing(*args, **kwargs): raise KeyError('not your report')
    monkeypatch.setattr(StrategyService, 'detail', missing)
    path = '/api/v1/web/strategies/' + str(uuid4())
    assert client.get(path).status_code == 404
    def conflict(*args, **kwargs): raise ValueError('策略版本已变化')
    monkeypatch.setattr(StrategyService, 'detail', conflict)
    assert client.get(path).status_code == 422
    def failure(*args, **kwargs): raise RuntimeError('postgresql://user:secret@host')
    monkeypatch.setattr(StrategyService, 'detail', failure)
    response = client.get(path)
    assert response.status_code == 503 and 'secret' not in response.text
