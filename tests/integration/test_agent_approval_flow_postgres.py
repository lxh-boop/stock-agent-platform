"""Approval dispatch, restart recovery and Worker claims against real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import pytest

from agent_graph.approval_store import ApprovalStore, thread_id_for_run
from application.agent_approval_service import execute_approval_task, list_approval_actions, submit_approval_action
from core.user_scope import user_scope

pytestmark = pytest.mark.skipif(os.environ.get('STOCK_TEST_DATABASE') != '1', reason='local test database only')
OWNER = 'approval_flow_alice'


@pytest.fixture(autouse=True)
def schema(isolated_postgres_schema):
    yield


@pytest.fixture
def manager(monkeypatch):
    from server.api import tasks as api
    from server.task_runtime.store import TaskStore
    class Manager:
        def __init__(self):
            self.store, self.count = TaskStore(), 0
        def submit(self, **kwargs):
            self.count += 1
            return self.store.create(task_id='task_' + uuid4().hex, task_type=kwargs['task_type'],
                owner_id=kwargs['owner_id'], session_id=kwargs['session_id'],
                request={'kwargs': kwargs['kwargs']}, metadata=kwargs['metadata'])
    fake = Manager()
    monkeypatch.setattr(api, 'task_manager', fake)
    monkeypatch.setattr('application.user_credentials_service.get_user_llm_settings', lambda owner: None)
    return fake


def pending(*, session=None, run=None, call='one'):
    session, run = session or uuid4().hex, run or uuid4().hex
    return ApprovalStore().save_pending(user_id=OWNER, session_id=session, run_id=run,
        thread_id=thread_id_for_run(OWNER, session, run), interrupts=[{
            'id': call, 'value': {'name': 'run_strategy_backtest', 'args': {'name': '趋势'}}}])


def submit(row, decision='approved', **overrides):
    return submit_approval_action(**({'approval_id': row['approval_id'], 'user_id': OWNER,
        'session_id': row['session_id'], 'decision': decision, 'request_id': 'request-1',
        'idempotency_key': 'idempotency-1'} | overrides))


def execute(row, task, monkeypatch):
    monkeypatch.setenv('STOCK_AGENT_TASK_ID', task['task_id'])
    with user_scope(OWNER):
        return execute_approval_task(approval_id=row['approval_id'], user_id=OWNER, session_id=row['session_id'])


def test_concurrent_confirm_only_dispatches_one_scoped_task(manager):
    row = pending()
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: submit(row), range(4)))
    assert manager.count == 1 and len({task['task_id'] for task in result}) == 1
    with pytest.raises(ValueError, match='decision'):
        submit(row, 'rejected')
    with pytest.raises(KeyError):
        submit(row, user_id='approval_flow_bob')
    with pytest.raises(KeyError):
        submit(row, session_id='other-session')


@pytest.mark.parametrize('decision', ['approved', 'rejected'])
def test_worker_preserves_decision_and_duplicate_click_replays_result(manager, monkeypatch, decision):
    row = pending()
    task = submit(row, decision)
    calls = []
    def resume(**kwargs):
        assert kwargs['decision'] == decision
        kwargs['before_invoke']()
        # No lock is held while the model or tool executes: another HTTP
        # confirmation can immediately read and return the existing Task.
        assert submit(row, decision)['task_id'] == task['task_id']
        calls.append(kwargs)
        return {'success': True, 'answer': '已处理', 'run_id': row['run_id']}
    monkeypatch.setattr('agent_graph.service.resume_langgraph_agent', resume)
    result = execute(row, task, monkeypatch)
    manager.store.update(task['task_id'], status='succeeded', result=result)
    assert submit(row, decision)['task_id'] == task['task_id']
    assert execute(row, task, monkeypatch) == result
    assert len(calls) == 1
    assert list_approval_actions(user_id=OWNER, session_id=row['session_id']) == []


def test_new_interrupt_and_new_run_never_reuse_previous_approval(manager, monkeypatch):
    row = pending()
    task = submit(row)
    next_rows = []
    def resume(**kwargs):
        kwargs['before_invoke']()
        next_rows.append(pending(session=row['session_id'], run=row['run_id'], call='next'))
        return {'success': True, 'result': {'requires_confirmation': True,
                                          'plan_id': next_rows[0]['approval_id']}}
    monkeypatch.setattr('agent_graph.service.resume_langgraph_agent', resume)
    result = execute(row, task, monkeypatch)
    manager.store.update(task['task_id'], status='succeeded', result=result)
    assert submit(row)['task_id'] == task['task_id']
    assert manager.count == 1
    next_row = next_rows[0]
    assert next_row['approval_id'] != row['approval_id']
    assert ApprovalStore().get(next_row['approval_id'], user_id=OWNER, session_id=row['session_id'])['decision'] == ''
    other_run = pending(session=row['session_id'])
    assert other_run['thread_id'] != row['thread_id']
    active = list_approval_actions(user_id=OWNER, session_id=row['session_id'])
    assert {item['approval_id'] for item in active} == {next_row['approval_id'], other_run['approval_id']}


def test_preflight_failure_can_retry_but_invocation_failure_cannot_replay(manager, monkeypatch):
    row = pending()
    task = submit(row)
    def fail_before(**kwargs):
        raise RuntimeError('provider unavailable before invocation')
    monkeypatch.setattr('agent_graph.service.resume_langgraph_agent', fail_before)
    with pytest.raises(RuntimeError, match='preflight'):
        execute(row, task, monkeypatch)
    manager.store.update(task['task_id'], status='failed')
    assert list_approval_actions(user_id=OWNER, session_id=row['session_id'])[0]['can_retry']
    new_task = submit(row)
    assert new_task['task_id'] != task['task_id']
    def fail_after(**kwargs):
        kwargs['before_invoke']()
        raise RuntimeError('unknown result after invocation')
    monkeypatch.setattr('agent_graph.service.resume_langgraph_agent', fail_after)
    with pytest.raises(RuntimeError, match='after_invocation'):
        execute(row, new_task, monkeypatch)
    manager.store.update(new_task['task_id'], status='interrupted')
    item = list_approval_actions(user_id=OWNER, session_id=row['session_id'])[0]
    assert item['state'] == 'blocked' and not item['can_retry']
    with pytest.raises(ValueError, match='uncertain'):
        submit(row)
    assert manager.count == 2


@pytest.mark.parametrize('phase, expected', [('preparing', 'retryable'), ('invoking', 'blocked')])
def test_killed_worker_is_reconciled_from_task_state(manager, phase, expected):
    row = pending()
    task = submit(row)
    with ApprovalStore().locked(row['approval_id'], user_id=OWNER, session_id=row['session_id']) as entry:
        entry.update(state='running', phase=phase)
    manager.store.update(task['task_id'], status='interrupted')
    item = list_approval_actions(user_id=OWNER, session_id=row['session_id'])[0]
    assert item['state'] == expected
    assert item['can_retry'] == (phase == 'preparing')


def test_crash_after_task_persist_before_recording_id_reuses_the_task(manager, monkeypatch):
    row = pending()
    real_submit = manager.submit
    def interrupted_submit(**kwargs):
        real_submit(**kwargs)
        raise RuntimeError('crash after task persisted')
    monkeypatch.setattr(manager, 'submit', interrupted_submit)
    with pytest.raises(RuntimeError, match='submission_failed'):
        submit(row)
    task = submit(row)
    assert task['status'] == 'queued' and manager.count == 1


def test_crash_after_queued_commit_before_task_creation_is_retryable(manager):
    row = pending()
    with ApprovalStore().locked(row['approval_id'], user_id=OWNER, session_id=row['session_id']) as entry:
        entry.update(state='queued', phase='preparing', decision='approved')
    item = list_approval_actions(user_id=OWNER, session_id=row['session_id'])[0]
    assert item['state'] == 'retryable' and item['can_retry']
    assert submit(row)['status'] == 'queued' and manager.count == 1


def test_worker_refuses_wrong_task_context_and_preparing_claim_duplicate(manager, monkeypatch):
    row = pending()
    task = submit(row)
    other = pending()
    monkeypatch.setenv('STOCK_AGENT_TASK_ID', task['task_id'])
    with user_scope(OWNER), pytest.raises(PermissionError, match='scope'):
        execute_approval_task(approval_id=other['approval_id'], user_id=OWNER, session_id=other['session_id'])
    with ApprovalStore().locked(row['approval_id'], user_id=OWNER, session_id=row['session_id']) as entry:
        entry.update(state='running', phase='preparing')
    with pytest.raises(ValueError, match='already_claimed'):
        execute(row, task, monkeypatch)


def test_real_postgres_checkpoint_two_interrupts_approve_then_reject(manager, monkeypatch):
    """Persist a real LangGraph interrupt and resume it through the Task service.

    Only the LLM and deterministic write are offline stubs. The graph, HITL,
    PostgreSQL checkpointer, approval rows and Task records are real.
    """
    from types import SimpleNamespace
    from unittest.mock import Mock
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.tools import StructuredTool
    import agent_graph.graph as graph_module
    import agent_graph.service as graph_service
    from agent_graph.memory import open_checkpointer
    from tests.unit.test_agent_graph_engine import _tool_loop_model

    session, run = uuid4().hex, uuid4().hex
    thread = thread_id_for_run(OWNER, session, run)
    executed = []
    def record_value(value: str) -> str:
        """Record an approved test value."""
        executed.append(value)
        return 'saved:' + value
    tool = StructuredTool.from_function(record_value, name='save_factor_strategy')
    monkeypatch.setattr(graph_module, 'build_read_tools', lambda *args: [tool])
    monkeypatch.setattr(graph_module, 'build_main_capability_tools', lambda *args: [])
    monkeypatch.setattr(graph_module, 'build_research_docs_tools', lambda *args: [])
    resources = SimpleNamespace(tool_registry=None, context={'user_id': OWNER}, close=lambda: None)
    monkeypatch.setattr(graph_service.AgentResources, 'build', lambda **kwargs: resources)
    monkeypatch.setattr(graph_service, 'GraphRunTracer', lambda **kwargs: Mock(run_id=kwargs['run_id']))
    initial_model = _tool_loop_model([AIMessage(content='', tool_calls=[{
        'name': 'save_factor_strategy', 'args': {'value': 'first'}, 'id': 'call-first'}])])
    config = {'configurable': {'thread_id': thread, 'user_id': OWNER, 'run_id': run}}
    with user_scope(OWNER), open_checkpointer() as saver:
        graph = graph_module.build_agent_graph(initial_model, resources, {}, checkpointer=saver)
        state = graph.invoke({'user_id': OWNER, 'session_id': session, 'run_id': run,
                              'query': '两次写入', 'messages': [HumanMessage(content='请求两次写入')]}, config=config)
        response = graph_service._compose_response(final_state=state, query='两次写入', user_id=OWNER,
            session_id=session, language='zh', run_id=run, run_started=0, run_meta={}, tracer=Mock())
    assert not executed
    first = ApprovalStore().get(response['result']['plan_id'], user_id=OWNER, session_id=session)
    first_task = submit(first)
    next_model = _tool_loop_model([AIMessage(content='', tool_calls=[{
        'name': 'save_factor_strategy', 'args': {'value': 'second'}, 'id': 'call-second'}])])
    monkeypatch.setattr(graph_service, '_resolve_model', lambda *args, **kwargs: (None, next_model))
    next_response = execute(first, first_task, monkeypatch)
    manager.store.update(first_task['task_id'], status='succeeded', result=next_response)
    assert executed == ['first']
    assert next_response['result']['requires_confirmation']
    second_id = next_response['result']['plan_id']
    assert second_id != first['approval_id']
    assert submit(first)['task_id'] == first_task['task_id']
    second = ApprovalStore().get(second_id, user_id=OWNER, session_id=session)
    second_task = submit(second, 'rejected')
    final_model = _tool_loop_model([AIMessage(content='第二次写入已拒绝。')])
    monkeypatch.setattr(graph_service, '_resolve_model', lambda *args, **kwargs: (None, final_model))
    final = execute(second, second_task, monkeypatch)
    assert final['success'] and not final['result']['requires_confirmation']
    assert executed == ['first']
