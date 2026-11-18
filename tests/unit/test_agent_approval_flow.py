from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from agent_graph import service as graph_service
from agent_graph.approval_store import interrupt_signature, thread_id_for_run
from application.web_agent_service import WebAgentApplicationService


def interrupt(ident='interrupt-1', value=None):
    return SimpleNamespace(id=ident, value=value or {
        'name': 'run_strategy_backtest', 'args': {'spec': {'name': '趋势'}}, 'description': '回测趋势'})


def test_compose_persists_versioned_approval_and_all_parameters(monkeypatch):
    store = Mock()
    store.save_pending.return_value = {'approval_id': 'lg_approve_bound'}
    monkeypatch.setattr(graph_service, 'ApprovalStore', lambda: store)
    state = {'__interrupt__': (interrupt(), interrupt('interrupt-2', {
        'action_requests': [{'name': 'save_memory', 'args': {'content': '低换手'}, 'description': '记录偏好'}]}))}
    response = graph_service._compose_response(final_state=state, query='研究', user_id='alice',
        session_id='session', language='zh', run_id='run-1', run_started=0, run_meta={}, tracer=Mock())
    assert response['result']['plan_id'] == 'lg_approve_bound'
    assert response['result']['requires_confirmation'] is True
    assert len(response['result']['data']['pending_approval']['actions']) == 2
    saved = store.save_pending.call_args.kwargs
    assert saved['thread_id'] == thread_id_for_run('alice', 'session', 'run-1')
    assert saved['interrupts'][0]['value']['args']['spec']['name'] == '趋势'


@pytest.fixture
def resume_runtime(monkeypatch):
    pending = interrupt()
    saved = {'approval_id': 'lg_approve_bound', 'user_id': 'alice', 'session_id': 'session',
             'run_id': 'run-1', 'thread_id': thread_id_for_run('alice', 'session', 'run-1'),
             'decision': 'approved', 'payload_hash': interrupt_signature([{'id': pending.id, 'value': pending.value}])}
    snapshot = SimpleNamespace(values={'user_id': 'alice', 'session_id': 'session', 'run_id': 'run-1'},
                               tasks=[SimpleNamespace(interrupts=[pending])])
    graph = Mock()
    graph.get_state.return_value = snapshot
    graph.invoke.return_value = {'messages': [AIMessage(content='执行完成')], **snapshot.values}
    resources = Mock()
    monkeypatch.setattr(graph_service, '_resolve_model', lambda *a, **kw: (None, Mock()))
    monkeypatch.setattr(graph_service.AgentResources, 'build', lambda **kw: resources)
    monkeypatch.setattr(graph_service, 'GraphRunTracer', lambda **kw: Mock(run_id=kw['run_id']))
    monkeypatch.setattr(graph_service, 'build_agent_graph', lambda *a, **kw: graph)
    @contextmanager
    def checkpointer():
        yield Mock()
    monkeypatch.setattr(graph_service, 'open_checkpointer', checkpointer)
    return saved, snapshot, graph, resources


@pytest.mark.parametrize('mutation', ['owner', 'session', 'run', 'arguments', 'interrupt_id'])
def test_resume_rechecks_checkpoint_scope_and_parameters_before_invoking(resume_runtime, mutation):
    saved, snapshot, graph, _ = resume_runtime
    if mutation in {'owner', 'session', 'run'}:
        snapshot.values[{'owner': 'user_id', 'session': 'session_id', 'run': 'run_id'}[mutation]] = 'wrong'
    elif mutation == 'arguments':
        snapshot.tasks[0].interrupts[0].value['args']['spec']['name'] = 'changed'
    else:
        snapshot.tasks[0].interrupts[0].id = 'changed'
    before = Mock()
    with pytest.raises((PermissionError, ValueError)):
        graph_service.resume_langgraph_agent(session_id='session', user_id='alice', run_id='run-1',
            decision='approved', approval_record=saved, before_invoke=before)
    before.assert_not_called()
    graph.invoke.assert_not_called()


@pytest.mark.parametrize('decision, expected', [('approved', 'approve'), ('rejected', 'reject')])
def test_resume_only_answers_the_reviewed_interrupts(resume_runtime, decision, expected):
    saved, _, graph, resources = resume_runtime
    saved['decision'] = decision
    before = Mock()
    result = graph_service.resume_langgraph_agent(session_id='session', user_id='alice', run_id='run-1',
        decision=decision, approval_record=saved, before_invoke=before)
    assert result['success'] is True
    before.assert_called_once()
    command = graph.invoke.call_args.args[0]
    assert command.resume['interrupt-1']['type'] == expected
    assert graph.get_state.call_args.args[0]['configurable']['thread_id'] == saved['thread_id']
    resources.close.assert_called_once()


def web_service():
    service = WebAgentApplicationService.__new__(WebAgentApplicationService)
    service._output_dir, service.db_path = Path('outputs'), None
    service.agent = Mock()
    service.agent.get_user_conversation.return_value = {'conversation_id': 'session', 'language': 'zh'}
    service.agent.upsert_message.side_effect = lambda value: value
    return service


def test_web_pending_actions_exposes_scoped_approval_and_recovery_fields(monkeypatch):
    import application.agent_approval_service as approvals
    import application.web_agent_service as web
    proposal_store = Mock()
    proposal_store.list_pending.return_value = []
    monkeypatch.setattr(web, 'ProposalStore', lambda **kw: proposal_store)
    list_actions = Mock(return_value=[{
        'approval_id': 'lg_approve_bound', 'payload_hash': 'digest', 'session_id': 'session', 'run_id': 'run',
        'interrupts': [{'id': 'i', 'value': {'name': 'save_memory', 'args': {'content': '低换手'}}}],
        'decision': 'approved', 'state': 'retryable', 'created_at': 'now', 'expires_at': 'later',
        'task_id': 'task-r', 'can_retry': True, 'error': '准备检查失败',
    }])
    monkeypatch.setattr(approvals, 'list_approval_actions', list_actions)
    result = web_service().pending_actions('alice', conversation_id='session')
    item = result['records'][0]
    list_actions.assert_called_once_with(user_id='alice', session_id='session')
    assert item['proposal_id'] == item['plan_id'] == 'lg_approve_bound'
    assert item['confirmation_phrase'] == 'CONFIRM-_BOUND'
    assert item['decision'] == 'approved' and item['can_retry'] and item['task_id'] == 'task-r'
    assert item['proposed_changes'][0]['args']['content'] == '低换手'


def test_web_confirm_submits_background_and_reject_never_auto_approves(monkeypatch):
    import application.agent_approval_service as approvals
    submit = Mock(return_value={'task_id': 'task-r', 'owner_id': 'alice', 'session_id': 'session', 'status': 'queued'})
    monkeypatch.setattr(approvals, 'submit_approval_action', submit)
    service = web_service()
    request = dict(user_id='alice', conversation_id='session', plan_id='lg_approve_bound',
                   request_id='request', idempotency_key='idem')
    with pytest.raises(ValueError, match='confirmation_text'):
        service.control_pending_action(action='confirm', confirmation_text='incorrect', **request)
    submit.assert_not_called()
    result = service.control_pending_action(action='confirm', confirmation_text='CONFIRM-_BOUND', **request)
    assert result['task']['task_id'] == 'task-r'
    assert submit.call_args.kwargs['decision'] == 'approved'
    service.control_pending_action(action='reject', **request)
    assert submit.call_args.kwargs['decision'] == 'rejected'


def test_resumed_task_finalization_preserves_new_approval_for_refresh():
    service = web_service()
    result = service.finalize_task(user_id='alice', conversation_id='session', task={
        'task_id': 'task_resume1', 'task_type': 'agent.resume', 'owner_id': 'alice', 'session_id': 'session',
        'status': 'succeeded', 'result': {'success': True, 'run_id': 'run-1', 'answer': '请批准下一项',
            'result': {'requires_confirmation': True, 'plan_id': 'lg_approve_next',
                       'data': {'pending_approval': {'summary': '保存策略'}}}},
    })
    assert result['result_summary']['plan_id'] == 'lg_approve_next'
    assert result['result_summary']['requires_confirmation'] is True
    assert service.agent.upsert_message.call_args.args[0]['metadata']['task_id'] == 'task_resume1'


def test_resume_handler_is_registered_and_uses_cancel_callback(monkeypatch):
    import application.agent_approval_service as approvals
    from server.task_runtime.handlers import execute_task
    from server.task_runtime.manager import ALLOWED_TASK_TYPES
    run = Mock(return_value={'success': True})
    monkeypatch.setattr(approvals, 'execute_approval_task', run)
    cancel = lambda: False
    assert 'agent.resume' in ALLOWED_TASK_TYPES
    result = execute_task('agent.resume', {'kwargs': {'approval_id': 'p', 'user_id': 'alice', 'session_id': 's'}},
                          emit=Mock(), is_cancelled=cancel)
    assert result == {'success': True}
    run.assert_called_once_with(approval_id='p', user_id='alice', session_id='s', cancelled=cancel)


def test_pending_expiry_between_list_and_lock_does_not_break_other_records(monkeypatch):
    import application.agent_approval_service as approvals
    store = Mock()
    store.list_pending.return_value = [{'approval_id': 'expired-now', 'session_id': 'session'}]
    @contextmanager
    def expired(*args, **kwargs):
        raise ValueError('approval_expired_create_new_request')
        yield
    store.locked = expired
    monkeypatch.setattr(approvals, 'ApprovalStore', lambda: store)
    assert approvals.list_approval_actions(user_id='alice', session_id='session') == []
