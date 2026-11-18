import json
import pytest
from core.user_scope import current_owner, user_scope
from agent_graph.tools.backtest_tools import build_backtest_tools
from application.backtest_service import BacktestService
from application.strategy_service import StrategyService

CONTEXT = {'user_id': 'alice', 'session_id': 'session-test', 'run_id': 'run-test'}


def invoke(tool, args, call_id='research-call'):
    result = tool.invoke({'type':'tool_call','name':tool.name,'id':call_id,'args':args})
    return json.loads(result.content)

def test_agent_uses_same_service_with_bound_owner(monkeypatch):
    calls=[]
    def run(spec, **kwargs):
        calls.append((current_owner(),spec))
        return {'task_id':'real-task','task_type':'backtest.run','owner_id':'alice','status':'queued'}
    monkeypatch.setattr(BacktestService,'submit',run)
    tools={t.name:t for t in build_backtest_tools(CONTEXT)}
    args={'spec':{'start_date':'2026-01-01','end_date':'2026-01-30','factors':[{'field':'KMID'}]}}
    with user_scope('alice'):
        output=invoke(tools['run_strategy_backtest'], args)
    assert output['data']['task_id']=='real-task' and output['data']['status']=='queued'
    assert calls[0][0]=='alice'
    with user_scope('bob'):
        assert invoke(tools['run_strategy_backtest'], args)['success'] is False
    assert len(calls)==1


def test_agent_missing_data_no_fake_success(monkeypatch):
    def run(_, **kwargs): raise ValueError('个人因子库为空')
    monkeypatch.setattr(BacktestService,'submit',run)
    tool=next(t for t in build_backtest_tools(CONTEXT) if t.name=='run_strategy_backtest')
    value=invoke(tool, {'spec':{'start_date':'2026-01-01','end_date':'2026-01-30','factors':[{'field':'KMID'}]}})
    assert value['success'] is False and 'metrics' not in value


@pytest.mark.parametrize('name,kind', [('analyze_factors','analysis'),('screen_factor_strategy','screen')])
def test_all_research_tools_use_same_owned_service(monkeypatch,name,kind):
    seen=[]
    def run(spec,**kw):
        seen.append((current_owner(),kw['kind'],spec))
        return {'task_id':'real-task','task_type':'backtest.run','owner_id':'alice','status':'queued'}
    monkeypatch.setattr(BacktestService,'submit',run)
    tools={t.name:t for t in build_backtest_tools(CONTEXT)}
    dates={'start_date':'2026-01-05','end_date':'2026-01-05' if kind=='screen' else '2026-01-30'}
    with user_scope('alice'):
        result=invoke(tools[name], {'spec':{**dates,'factors':[{'field':'KMID'}]}})
    assert result['success'] and seen[0][0:2]==('alice',kind)


def test_agent_strategy_storage_is_user_scoped_and_catalog_has_no_sql(monkeypatch):
    seen=[]
    def save(ident,spec,**kwargs):
        seen.append(current_owner())
        return {'strategy_id':ident,'spec':spec}
    monkeypatch.setattr(StrategyService,'save',save)
    tools={t.name:t for t in build_backtest_tools(CONTEXT)}
    result=invoke(tools['save_factor_strategy'], {'spec':{'start_date':'2026-01-05','end_date':'2026-02-05','factors':[{'field':'MA5'}]}})
    assert result['success'] and seen==['alice'] and not current_owner()
    with user_scope('bob'):
        assert not invoke(tools['save_factor_strategy'], {'spec':{'start_date':'2026-01-05','end_date':'2026-02-05','factors':[{'field':'MA5'}]}})['success']
    assert len(seen)==1


def params():
    return {'start_date':'2026-01-05','end_date':'2026-02-05','factors':[{'field':'MA5'}]}


def test_task_ids_are_stable_per_runtime_call_and_not_model_parameters(monkeypatch):
    requests=[]
    def submit(spec, **kwargs):
        requests.append(kwargs)
        return {'owner_id':'alice','task_type':'backtest.run','task_id':'queued-task','status':'queued'}
    monkeypatch.setattr(BacktestService,'submit',submit)
    tool=next(t for t in build_backtest_tools(CONTEXT) if t.name=='run_strategy_backtest')
    assert 'tool_call_id' not in tool.tool_call_schema.model_json_schema()['properties']
    invoke(tool, {'spec':params()})
    invoke(tool, {'spec':params()})
    invoke(tool, {'spec':params(),'tool_call_id':'model-spoof'})
    invoke(tool, {'spec':params()},call_id='different')
    other=next(t for t in build_backtest_tools({**CONTEXT,'run_id':'other-run'}) if t.name=='run_strategy_backtest')
    invoke(other, {'spec':params()})
    assert requests[0]['request_id']==requests[1]['request_id']==requests[2]['request_id']
    assert len({r['request_id'] for r in requests})==3
    assert all(r['session_id']==CONTEXT['session_id'] for r in requests)


def test_strategy_edits_require_the_reviewed_version_and_stable_request(monkeypatch):
    calls=[]
    ident='11111111-1111-4111-8111-111111111111'
    def save(id,spec,**kwargs):
        calls.append((id,kwargs))
        return {'strategy_id':id,'version':kwargs['expected_version'],'spec':spec}
    monkeypatch.setattr(StrategyService,'save',save)
    tools={t.name:t for t in build_backtest_tools(CONTEXT)}
    assert not invoke(tools['save_factor_strategy'],{'strategy_id':ident,'spec':params()})['success']
    assert not calls
    args={'strategy_id':ident,'expected_version':3,'spec':params()}
    assert invoke(tools['save_factor_strategy'],args)['success']
    assert invoke(tools['save_factor_strategy'],args)['success']
    assert calls[0]==calls[1] and calls[0][1]['expected_version']==3


def test_only_successful_owned_tasks_load_reports(monkeypatch):
    from types import SimpleNamespace
    from server.api import tasks
    from backtesting import store
    report_id='22222222-2222-4222-8222-222222222222'
    row={'task_id':'task-1','task_type':'backtest.run','owner_id':'alice','status':'running','result':{'run_id':report_id}}
    class TaskStore:
        def get(self,id): return dict(row)
    monkeypatch.setattr(tasks,'task_manager',SimpleNamespace(store=TaskStore()))
    reports=[]
    monkeypatch.setattr(store,'get_report',lambda id,**kw: reports.append(id) or {'run_id':id,'metrics':{'total_return':.25}})
    tool=next(t for t in build_backtest_tools(CONTEXT) if t.name=='get_strategy_backtest')
    assert invoke(tool,{'task_id':'task-1'})['data']['status']=='running'
    assert not reports
    row['status']='succeeded'
    assert invoke(tool,{'task_id':'task-1'})['data']['report']['metrics']['total_return']==.25
    row['owner_id']='bob'
    assert not invoke(tool,{'task_id':'task-1'})['success']
    assert len(reports)==1


def test_strategy_from_report_rerun_delete_bind_ids_versions_and_owner(monkeypatch):
    ident='11111111-1111-4111-8111-111111111111'
    calls=[]
    def from_report(run_id,**kwargs):
        calls.append(('save',current_owner(),run_id,kwargs))
        return {'strategy_id':ident,'version':1,'spec':{'name':'saved'},'latest_run_id':run_id}
    def rerun(strategy_id,**kwargs):
        calls.append(('rerun',current_owner(),strategy_id,kwargs))
        return {'task_id':'task-rerun','task_type':'backtest.run','owner_id':'alice','status':'queued'}
    def delete(strategy_id,**kwargs):
        calls.append(('delete',current_owner(),strategy_id,kwargs))
        return {'deleted':True}
    monkeypatch.setattr(StrategyService,'from_report',from_report)
    monkeypatch.setattr(StrategyService,'rerun',rerun)
    monkeypatch.setattr(StrategyService,'delete',delete)
    tools={t.name:t for t in build_backtest_tools(CONTEXT)}
    assert invoke(tools['save_strategy_from_report'],{'run_id':ident})['data']['latest_run_id']==ident
    for name in ('rerun_factor_strategy','delete_factor_strategy'):
        assert invoke(tools[name],{'strategy_id':ident,'expected_version':2})['success']
    assert all(call[1]=='alice' for call in calls)
    assert calls[1][3]['expected_version']==calls[2][3]['expected_version']==2
    assert calls[1][3]['session_id']==CONTEXT['session_id']


def test_strategy_list_is_compact_and_explicit_detail_has_parameters(monkeypatch):
    item={'strategy_id':'test-id','version':2,'spec':{'name':'test','factors':[{'field':'MA5'}]},
          'latest_report':{'metrics':{'total_return':.1},'equity':[{'large':'x'*10000}]*10}}
    monkeypatch.setattr(StrategyService,'list',lambda **kw:{'records':[item],'total':25,'stats':{'total':25}})
    tools={t.name:t for t in build_backtest_tools(CONTEXT)}
    result=invoke(tools['list_factor_strategies'],{})['data']
    assert result['next_offset']==20 and result['records'][0]['metrics']['total_return']==.1
    assert 'equity' not in json.dumps(result)


def test_agent_can_read_bounded_real_factor_correlation_and_other_report_rows(monkeypatch):
    from backtesting import store
    calls=[]
    def rows(ident,kind,limit,offset):
        calls.append((current_owner(),ident,kind,limit,offset))
        return {'records':[{'factor_a':'MA5','factor_b':'KMID','correlation':.75}], 'total':51,'offset':offset,'limit':limit}
    monkeypatch.setattr(store,'report_rows',rows)
    tool=next(t for t in build_backtest_tools(CONTEXT) if t.name=='get_strategy_backtest')
    ident='11111111-1111-4111-8111-111111111111'
    result=invoke(tool,{'run_id':ident,'row_kind':'correlation','limit':20,'offset':20})
    assert result['data']['records'][0]['correlation']==.75
    assert calls==[('alice',ident,'correlation',20,20)]
    with user_scope('bob'):
        assert not invoke(tool,{'run_id':ident,'row_kind':'correlation'})['success']
    assert len(calls)==1
    assert not invoke(tool,{'row_kind':'correlation'})['success']
    from pydantic import ValidationError
    with pytest.raises(ValidationError): invoke(tool,{'run_id':ident,'row_kind':'correlation','limit':500000})
    with pytest.raises(ValidationError): invoke(tool,{'run_id':ident,'row_kind':'user_account'})
