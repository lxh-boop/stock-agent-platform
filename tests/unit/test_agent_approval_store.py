from copy import deepcopy

import pytest

from agent_graph.approval_store import ApprovalStore, interrupt_signature, thread_id_for_run
from core.user_scope import user_scope


def test_thread_identity_is_owner_session_and_run_scoped():
    base = thread_id_for_run('alice', 'session', 'run')
    assert base == thread_id_for_run('alice', 'session', 'run')
    assert len({base, thread_id_for_run('bob', 'session', 'run'),
                thread_id_for_run('alice', 'other', 'run'), thread_id_for_run('alice', 'session', 'other')}) == 4
    with user_scope('alice'), pytest.raises(PermissionError):
        thread_id_for_run('bob', 'session', 'run')


def test_interrupt_digest_binds_ids_and_approved_arguments():
    values = [{'id': 'a', 'value': {'name': 'save_factor_strategy', 'args': {'name': 'original'}}},
              {'id': 'b', 'value': {'name': 'save_memory', 'args': {'content': 'preference'}}}]
    signature = interrupt_signature(values)
    assert interrupt_signature(list(reversed(values))) == signature
    changed = deepcopy(values)
    changed[0]['value']['args']['name'] = 'changed'
    assert interrupt_signature(changed) != signature
    changed = deepcopy(values)
    changed[0]['id'] = 'new-interrupt'
    assert interrupt_signature(changed) != signature
    with pytest.raises(ValueError):
        interrupt_signature([values[0], values[0]])
    with pytest.raises(ValueError):
        interrupt_signature([{'id': 'a', 'value': {'huge': 'x' * 300000}}])
    with pytest.raises(ValueError):
        interrupt_signature([{'id': 'a', 'value': {'bad': float('nan')}}])


def test_constructor_never_connects_and_invalid_scope_fails_before_database(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError('must not connect')
    monkeypatch.setattr('agent_graph.approval_store.get_connection', forbidden)
    store = ApprovalStore()
    with user_scope('alice'), pytest.raises(PermissionError):
        store.get('id', user_id='bob', session_id='s')
    with pytest.raises(ValueError):
        store.save_pending(user_id='alice', session_id='s', run_id='r', thread_id='unbound',
                           interrupts=[{'id': 'i', 'value': {}}])
