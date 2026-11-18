"""Real PostgreSQL checks for approval scope, versioning and concurrent claims."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import pytest

from agent_graph.approval_store import ApprovalStore, thread_id_for_run
from database.connection import get_connection

pytestmark = pytest.mark.skipif(os.environ.get('STOCK_TEST_DATABASE') != '1', reason='isolated local DB only')


@pytest.fixture(autouse=True)
def schema(isolated_postgres_schema):
    yield


def save(store, run=None, interrupts=None):
    run = run or uuid4().hex
    return store.save_pending(user_id='approval_alice', session_id='test-session', run_id=run,
        thread_id=thread_id_for_run('approval_alice', 'test-session', run),
        interrupts=interrupts or [{'id': 'call-a', 'value': {'name': 'run_strategy_backtest', 'args': {'name': 'trend'}}}])


def test_scope_version_idempotence_and_closed_decision():
    store = ApprovalStore()
    first = save(store)
    ident = first['approval_id']
    with pytest.raises(KeyError):
        store.get(ident, user_id='approval_bob', session_id='test-session')
    with pytest.raises(KeyError):
        store.get(ident, user_id='approval_alice', session_id='other-session')
    assert save(store, first['run_id'])['approval_id'] == ident
    with store.locked(ident, user_id='approval_alice', session_id='test-session') as entry:
        entry.update(decision='approved', state='queued', request_id='request-1', task_id='task-1')
        with pytest.raises(ValueError, match='already_bound'):
            entry.update(decision='rejected')
        entry.update(state='completed', result={'task_id': 'task-1'})
        with pytest.raises(ValueError, match='closed'):
            entry.update(state='pending')
    repeated = save(store, first['run_id'])
    assert repeated['state'] == 'completed' and repeated['result'] == {'task_id': 'task-1'}
    second = save(store, first['run_id'], [{'id': 'call-b', 'value': {'name': 'save_factor_strategy', 'args': {}}}])
    assert second['approval_id'] != ident and second['state'] == 'pending'
    assert store.get(ident, user_id='approval_alice', session_id='test-session')['state'] == 'completed'
    assert ident not in {item['approval_id'] for item in store.list_pending(user_id='approval_alice')}


def test_new_interrupt_supersedes_old_without_reopening_it():
    store = ApprovalStore()
    first = save(store)
    second = save(store, first['run_id'], [{'id': 'changed', 'value': {'args': {'amount': 200}}}])
    assert save(store, first['run_id'])['state'] == 'superseded'
    assert store.get(second['approval_id'], user_id='approval_alice', session_id='test-session')['state'] == 'pending'


def test_session_lock_survives_commit_and_concurrent_clicks_claim_once():
    store = ApprovalStore()
    approval = save(store)
    claims = []

    def click(index):
        with store.locked(approval['approval_id'], user_id='approval_alice', session_id='test-session') as entry:
            if entry.row['state'] == 'pending':
                entry.update(state='queued', decision='approved', task_id=f'task-{index}')
                # The session lock stays owned even after the explicit commit.
                with get_connection(runtime=True) as probe:
                    key = 'approval-control:' + approval['approval_id']
                    acquired = probe.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok', (key,)).fetchone()['ok']
                    assert not acquired
                claims.append(index)
            return entry.row['task_id']

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(click, range(4)))
    assert len(claims) == 1 and len(set(results)) == 1


def test_committed_claim_survives_error_and_finds_task_in_crash_window():
    from server.task_runtime.store import TaskStore
    store = ApprovalStore()
    approval = save(store)
    task_id = 'task_' + uuid4().hex
    with pytest.raises(RuntimeError):
        with store.locked(approval['approval_id'], user_id='approval_alice', session_id='test-session') as entry:
            entry.update(decision='approved', state='queued')
            TaskStore().create(task_id=task_id, task_type='agent.resume', request={}, owner_id='approval_alice',
                               session_id='test-session', metadata={'approval_id': approval['approval_id']})
            raise RuntimeError('simulated crash before recording task ID')
    with store.locked(approval['approval_id'], user_id='approval_alice', session_id='test-session') as entry:
        assert entry.row['state'] == 'queued' and entry.row['task_id'] == ''
        assert entry.latest_task() == {'task_id': task_id, 'status': 'queued'}


def test_expired_approval_cannot_be_claimed_or_reopened():
    store = ApprovalStore()
    approval = save(store)
    with get_connection(runtime=True) as conn:
        conn.execute("UPDATE agent_tool_approval SET expires_at=CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE approval_id=%s", (approval['approval_id'],))
        conn.commit()
    with pytest.raises(ValueError, match='expired'):
        with store.locked(approval['approval_id'], user_id='approval_alice', session_id='test-session'):
            pytest.fail('expired approval entered')
    assert save(store, approval['run_id'])['state'] == 'expired'
