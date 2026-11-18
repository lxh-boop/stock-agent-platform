"""Actual PostgreSQL strategy versions and durable Task associations; no production data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from uuid import uuid4

import pytest
from psycopg import sql

from application.strategy_service import StrategyService
from backtesting import store, strategy_store
from backtesting.contracts import BacktestSpec
from core.user_scope import user_scope, user_schema
from database.connection import get_connection

pytestmark = pytest.mark.skipif(os.environ.get('STOCK_TEST_DATABASE') != '1', reason='isolated local DB only')
USERS = ('workspace_alice', 'workspace_bob')


def params(name='Trend'):
    return BacktestSpec.model_validate({'name': name, 'start_date': '2026-01-05', 'end_date': '2026-01-08',
        'factors': [{'field': 'KMID'}], 'initial_cash': 10000, 'max_positions': 1}).model_dump(mode='json')


def report(spec=None, *, annual=.2, sharpe=1.5):
    # Completed-report fixture validates storage linkage, not engine return math.
    return {'kind': 'backtest', 'spec': spec or params(), 'metrics': {'annual_return': annual, 'sharpe': sharpe},
            'equity': [{'date': '2026-01-05', 'nav': 1}, {'date': '2026-01-08', 'nav': 1.1}],
            'trades': [{'code': '000001', 'shares': 100}], 'engine_version': 'factor-event-v2'}


@pytest.fixture(autouse=True)
def prepare(isolated_postgres_schema):
    for owner in USERS:
        with user_scope(owner), get_connection() as conn:
            strategy_store.ensure(conn)
            store._reports(conn)
            names = ['backtest_strategy', 'backtest_strategy_revision', 'backtest_strategy_run', 'backtest_strategy_request', 'backtest_run']
            conn.execute(sql.SQL('TRUNCATE {}').format(sql.SQL(',').join(sql.Identifier(user_schema(owner), name) for name in names)))
            conn.commit()
    yield


@pytest.fixture
def manager(monkeypatch):
    from server.api import tasks as api
    from server.task_runtime.store import TaskStore
    class Manager:
        def __init__(self): self.store = TaskStore(); self.count = 0
        def submit(self, **kwargs):
            self.count += 1
            return self.store.create(task_id='task_' + uuid4().hex, task_type=kwargs['task_type'], owner_id=kwargs['owner_id'],
                session_id=kwargs.get('session_id', ''), request={'args': [], 'kwargs': kwargs['kwargs']}, metadata=kwargs['metadata'])
    value = Manager()
    with get_connection(runtime=True) as conn:
        conn.execute('DELETE FROM task_events WHERE task_id IN (SELECT task_id FROM task_runs WHERE owner_id=ANY(%s))', (list(USERS),))
        conn.execute('DELETE FROM task_runs WHERE owner_id=ANY(%s)', (list(USERS),))
        conn.commit()
    monkeypatch.setattr(api, 'task_manager', value)
    return value


def saved_source():
    run_id, request_id = str(uuid4()), str(uuid4())
    store.save_report(run_id, report())
    return run_id, StrategyService.from_report(run_id, request_id=request_id)


def test_from_report_is_exact_idempotent_personal_and_leaves_accounts_untouched():
    with get_connection(scoped=False) as conn:
        before = conn.execute('SELECT * FROM paper_account ORDER BY account_id').fetchall()
    with user_scope(USERS[0]):
        run_id, strategy = saved_source()
        ident = strategy['strategy_id']
        assert strategy['latest_run_id'] == run_id and strategy['latest_run_version'] == strategy['version'] == 1
        assert strategy['latest_report']['equity'] == report()['equity']
        assert StrategyService.from_report(run_id, request_id=ident)['strategy_id'] == ident
        with pytest.raises(ValueError, match='不同'):
            StrategyService.from_report(run_id, request_id=ident, name='changed')
        second = StrategyService.from_report(run_id, request_id=str(uuid4()), name='Renamed')
        assert second['spec']['name'] == 'Renamed' and second['latest_report']['spec']['name'] == 'Trend'
    with user_scope(USERS[1]):
        assert StrategyService.list()['total'] == 0
        with pytest.raises(KeyError): StrategyService.detail(ident)
        with pytest.raises(KeyError): StrategyService.from_report(run_id, request_id=str(uuid4()))
    with get_connection(scoped=False) as conn:
        assert before == conn.execute('SELECT * FROM paper_account ORDER BY account_id').fetchall()


def test_edit_keeps_immutable_versions_and_identifies_old_report():
    with user_scope(USERS[0]):
        source, strategy = saved_source()
        ident, request_id = strategy['strategy_id'], str(uuid4())
        newer = params('New name')
        updated = StrategyService.save(ident, newer, request_id=request_id, expected_version=1)
        assert updated['version'] == 2 and updated['status'] == 'draft'
        assert updated['latest_run_id'] == source and updated['latest_run_version'] == 1
        assert StrategyService.save(ident, newer, request_id=request_id, expected_version=1)['version'] == 2
        with pytest.raises(ValueError, match='版本'):
            StrategyService.save(ident, params('stale'), request_id=str(uuid4()), expected_version=1)
        with pytest.raises(ValueError, match='版本'):
            StrategyService.save(ident, params('unchecked'), request_id=str(uuid4()))
        with get_connection() as conn:
            rows = conn.execute(sql.SQL('SELECT version,spec FROM {} ORDER BY version').format(strategy_store.table('backtest_strategy_revision'))).fetchall()
        assert [(row['version'], row['spec']['name']) for row in rows] == [(1, 'Trend'), (2, 'New name')]


def test_run_success_failure_cancel_and_refresh_recovery(manager):
    with user_scope(USERS[0]):
        source, strategy = saved_source()
        ident, request_id = strategy['strategy_id'], str(uuid4())
        task = StrategyService.rerun(ident, request_id=request_id, expected_version=1)
        assert StrategyService.rerun(ident, request_id=request_id, expected_version=1)['task_id'] == task['task_id']
        assert manager.count == 1
        running = StrategyService.detail(ident)
        assert running['status'] == 'queued' and running['active_task_id'] == task['task_id']
        with pytest.raises(ValueError, match='正在运行'):
            StrategyService.save(ident, params('changed'), request_id=str(uuid4()), expected_version=1)
        with pytest.raises(ValueError, match='正在运行'):
            StrategyService.delete(ident, request_id=str(uuid4()), expected_version=1)
        manager.store.update(task['task_id'], status='failed', error={'message': 'data missing'})
        failed = StrategyService.detail(ident)
        assert failed['status'] == 'failed' and failed['latest_run_id'] == source and failed['active_task_id'] is None
        success_id = str(uuid4())
        second = StrategyService.rerun(ident, request_id=success_id, expected_version=1)
        store.save_report(success_id, report())
        assert StrategyService.detail(ident)['latest_run_id'] == source  # Task has not succeeded yet.
        manager.store.update(second['task_id'], status='succeeded', result={'run_id': success_id})
        assert StrategyService.detail(ident)['latest_run_id'] == success_id
        # A concurrent refresh with an older Task snapshot cannot regress a closed run.
        strategy_store.reconcile(success_id, 'queued')
        with get_connection() as conn:
            assert conn.execute(sql.SQL('SELECT status FROM {} WHERE run_id=%s').format(strategy_store.table('backtest_strategy_run')), (success_id,)).fetchone()['status'] == 'succeeded'
        third = StrategyService.rerun(ident, request_id=str(uuid4()), expected_version=1)
        manager.store.update(third['task_id'], status='cancelled')
        cancelled = StrategyService.detail(ident)
        assert cancelled['status'] == 'cancelled' and cancelled['latest_run_id'] == success_id


def test_success_without_report_is_failed_and_wrong_version_report_is_rejected(manager):
    with user_scope(USERS[0]):
        source, strategy = saved_source()
        request_id = str(uuid4())
        task = StrategyService.rerun(strategy['strategy_id'], request_id=request_id, expected_version=1)
        with pytest.raises(ValueError, match='参数'):
            store.save_report(request_id, report(params('wrong parameters')))
        with pytest.raises(KeyError): store.get_report(request_id)
        manager.store.update(task['task_id'], status='succeeded')
        current = StrategyService.detail(strategy['strategy_id'])
        assert current['status'] == 'failed' and current['latest_run_id'] == source


def test_dispatch_crash_after_task_commit_is_recovered_before_edit_or_delete(manager, monkeypatch):
    from application.backtest_service import BacktestService
    original = BacktestService.submit
    def crash_after_persist(spec, **kwargs):
        original(spec, **kwargs)
        raise RuntimeError('simulated process loss after durable Task')
    monkeypatch.setattr(BacktestService, 'submit', crash_after_persist)
    with user_scope(USERS[0]):
        _, strategy = saved_source()
        ident, request_id = strategy['strategy_id'], str(uuid4())
        with pytest.raises(RuntimeError, match='simulated'):
            StrategyService.rerun(ident, request_id=request_id, expected_version=1)
        # The reservation survived; refresh finds the durable Task by owner/request UUID.
        restored = StrategyService.detail(ident)
        assert restored['status'] == 'queued' and restored['active_task_id']
        assert restored['task']['request']['kwargs']['run_id'] == request_id
        with pytest.raises(ValueError, match='正在运行'):
            StrategyService.delete(ident, request_id=str(uuid4()), expected_version=1)
        monkeypatch.setattr(BacktestService, 'submit', original)
        assert StrategyService.rerun(ident, request_id=request_id, expected_version=1)['task_id'] == restored['active_task_id']
        assert manager.count == 1


def test_dispatch_failure_before_task_is_retryable_with_same_request(manager, monkeypatch):
    from application.backtest_service import BacktestService
    original = BacktestService.submit
    def before_persist(*args, **kwargs): raise RuntimeError('no Task created')
    monkeypatch.setattr(BacktestService, 'submit', before_persist)
    with user_scope(USERS[0]):
        source, strategy = saved_source()
        ident, request_id = strategy['strategy_id'], str(uuid4())
        with pytest.raises(RuntimeError):
            StrategyService.rerun(ident, request_id=request_id, expected_version=1)
        failed = StrategyService.detail(ident)
        assert failed['status'] == 'interrupted' and failed['active_task_id'] is None and failed['latest_run_id'] == source
        monkeypatch.setattr(BacktestService, 'submit', original)
        task = StrategyService.rerun(ident, request_id=request_id, expected_version=1)
        assert StrategyService.detail(ident)['active_task_id'] == task['task_id']
        assert manager.count == 1


def test_refresh_rechecks_task_after_dispatch_finishes_between_lookups(manager, monkeypatch):
    from application.backtest_service import BacktestService
    with user_scope(USERS[0]):
        _, strategy = saved_source()
        ident, run_id = strategy['strategy_id'], str(uuid4())
        def interrupted_dispatch(spec):
            raise RuntimeError('reservation is durable but dispatch has not finished')
        with pytest.raises(RuntimeError):
            strategy_store.submit(ident, request_id=run_id, expected_version=1, callback=interrupted_dispatch)
        original_lookup = strategy_store.recover_task_id
        lookups, dispatched = [], []
        def old_snapshot_then_finish_dispatch(request_id):
            lookups.append(request_id)
            if len(lookups) == 1:
                assert original_lookup(request_id) is None
                # A concurrent dispatcher finishes after A's empty lookup, but
                # before A can acquire dispatch exclusion. No sleeps needed.
                task = BacktestService.submit(strategy['spec'], request_id=request_id)
                strategy_store.attach_task(request_id, task)
                dispatched.append(task['task_id'])
                return None
            return original_lookup(request_id)
        monkeypatch.setattr(strategy_store, 'recover_task_id', old_snapshot_then_finish_dispatch)
        current = StrategyService.detail(ident)
        assert lookups == [run_id, run_id]
        assert current['status'] == 'queued' and current['active_task_id'] == dispatched[0]
        assert current['task']['status'] == 'queued' and manager.count == 1


def test_legacy_http_routes_cannot_bypass_version_check():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.api.routers.web_backtests import router
    from server.api.security import AuthUser, require_user
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthUser(USERS[0], 'Alice', 'user')
    client = TestClient(app)
    with user_scope(USERS[0]):
        _, strategy = saved_source()
    ident = strategy['strategy_id']
    body = {'strategy_id': ident, 'request_id': str(uuid4()), 'confirmed': True, 'spec': params('unchecked')}
    assert client.put('/api/v1/web/backtests/strategies', json=body).status_code == 422
    assert client.request('DELETE', '/api/v1/web/backtests/strategies/' + ident,
                          json={'request_id': str(uuid4()), 'confirmed': True}).status_code == 422
    with user_scope(USERS[0]):
        assert StrategyService.detail(ident)['version'] == 1


def test_delete_is_version_checked_idempotent_and_preserves_report_and_history():
    with user_scope(USERS[0]):
        source, strategy = saved_source()
        ident, request_id = strategy['strategy_id'], str(uuid4())
        with pytest.raises(ValueError, match='版本'):
            StrategyService.delete(ident, request_id=request_id, expected_version=2)
        assert StrategyService.delete(ident, request_id=request_id, expected_version=1)['deleted']
        assert StrategyService.delete(ident, request_id=request_id, expected_version=1)['deleted']
        assert store.get_report(source)['trades']
        assert StrategyService.list()['total'] == 0
        with pytest.raises(ValueError, match='归档'):
            StrategyService.save(ident, params('recreate'), request_id=str(uuid4()))
        with get_connection() as conn:
            assert conn.execute(sql.SQL('SELECT COUNT(*) AS n FROM {}').format(strategy_store.table('backtest_strategy_revision'))).fetchone()['n'] == 1


def test_deleting_report_detaches_cards_without_deleting_strategy():
    with user_scope(USERS[0]):
        source, strategy = saved_source()
        store.delete_report(source)
        current = StrategyService.detail(strategy['strategy_id'])
        assert current['latest_report'] is None and current['latest_run_id'] is None and current['status'] == 'draft'


def test_stats_use_all_strategies_and_curves_are_bounded():
    with user_scope(USERS[0]):
        for index in range(3):
            run = str(uuid4())
            value = report(annual=index / 10, sharpe=index)
            value['equity'] = [{'date': f'day-{i}', 'nav': 1 + i / 1000} for i in range(1300)]
            store.save_report(run, value)
            StrategyService.from_report(run, request_id=str(uuid4()))
        listing = StrategyService.list(limit=1, offset=1)
        assert len(listing['records']) == 1 and listing['total'] == listing['stats']['total'] == 3
        assert listing['stats']['avg_annual_return'] == pytest.approx(.1)
        assert listing['stats']['avg_sharpe'] == 1
        curve = listing['records'][0]['latest_report']['equity']
        assert len(curve) <= 101 and curve[0]['date'] == 'day-0' and curve[-1]['date'] == 'day-1299'


def test_concurrent_same_request_saves_one_strategy():
    with user_scope(USERS[0]):
        run, request = str(uuid4()), str(uuid4())
        store.save_report(run, report())
    def save(_):
        with user_scope(USERS[0]):
            return StrategyService.from_report(run, request_id=request)['strategy_id']
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(save, range(4))) == {request}
    with user_scope(USERS[0]):
        assert StrategyService.list()['total'] == 1


def test_legacy_three_column_table_is_migrated_without_replacing_spec():
    owner = 'workspace_legacy'
    ident = str(uuid4())
    with user_scope(owner), get_connection() as conn:
        conn.execute(sql.SQL('DROP TABLE IF EXISTS {}').format(strategy_store.table('backtest_strategy')))
        conn.execute(sql.SQL('CREATE TABLE {} (strategy_id UUID PRIMARY KEY,spec JSONB NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)').format(strategy_store.table('backtest_strategy')))
        from psycopg.types.json import Jsonb
        conn.execute(sql.SQL('INSERT INTO {} (strategy_id,spec) VALUES(%s,%s)').format(strategy_store.table('backtest_strategy')), (ident, Jsonb(params('Legacy'))))
        conn.commit()
    with user_scope(owner):
        current = StrategyService.detail(ident)
        assert current['version'] == 1 and current['spec']['name'] == 'Legacy' and current['latest_run_id'] is None
