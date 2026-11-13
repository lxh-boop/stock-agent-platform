import json
import pytest
from core.user_scope import current_owner, user_scope
from agent_graph.tools.backtest_tools import build_backtest_tools
from application.backtest_service import BacktestService


def test_agent_uses_same_service_with_bound_owner(monkeypatch):
    calls=[]
    def run(spec, **kwargs):
        calls.append((current_owner(),spec))
        return {'run_id':'real-report','metrics':{'total_return':.125}}
    monkeypatch.setattr(BacktestService,'run',run)
    tools={t.name:t for t in build_backtest_tools({'user_id':'alice'})}
    args={'spec':{'start_date':'2026-01-01','end_date':'2026-01-30','factors':[{'field':'KMID'}]}}
    with user_scope('alice'):
        output=json.loads(tools['run_strategy_backtest'].invoke(args))
    assert output['data']['metrics']['total_return']==.125
    assert calls[0][0]=='alice'
    with user_scope('bob'):
        assert json.loads(tools['run_strategy_backtest'].invoke(args))['success'] is False
    assert len(calls)==1


def test_agent_missing_data_no_fake_success(monkeypatch):
    def run(_, **kwargs): raise ValueError('个人因子库为空')
    monkeypatch.setattr(BacktestService,'run',run)
    tool=next(t for t in build_backtest_tools({'user_id':'alice'}) if t.name=='run_strategy_backtest')
    value=json.loads(tool.invoke({'spec':{'start_date':'2026-01-01','end_date':'2026-01-30','factors':[{'field':'KMID'}]}}))
    assert value['success'] is False and 'metrics' not in value


@pytest.mark.parametrize('name,kind', [('analyze_factors','analysis'),('screen_factor_strategy','screen')])
def test_all_research_tools_use_same_owned_service(monkeypatch,name,kind):
    seen=[]
    def run(spec,**kw):
        seen.append((current_owner(),kw['kind'],spec))
        return {'kind':kw['kind'],'run_id':'saved-report'}
    monkeypatch.setattr(BacktestService,'run',run)
    tools={t.name:t for t in build_backtest_tools({'user_id':'alice'})}
    dates={'start_date':'2026-01-05','end_date':'2026-01-05' if kind=='screen' else '2026-01-30'}
    with user_scope('alice'):
        result=json.loads(tools[name].invoke({'spec':{**dates,'factors':[{'field':'KMID'}]}}))
    assert result['success'] and seen[0][0:2]==('alice',kind)


def test_agent_strategy_storage_is_user_scoped_and_catalog_has_no_sql(monkeypatch):
    from backtesting import store
    seen=[]
    def save(ident,spec):
        seen.append(current_owner())
        return {'strategy_id':ident,'spec':spec}
    monkeypatch.setattr(store,'save_strategy',save)
    tools={t.name:t for t in build_backtest_tools({'user_id':'alice'})}
    result=json.loads(tools['save_factor_strategy'].invoke({'spec':{'start_date':'2026-01-05','end_date':'2026-02-05','factors':[{'field':'MA5'}]}}))
    assert result['success'] and seen==['alice'] and not current_owner()
    with user_scope('bob'):
        assert not json.loads(tools['save_factor_strategy'].invoke({'spec':{'start_date':'2026-01-05','end_date':'2026-02-05','factors':[{'field':'MA5'}]}}))['success']
    assert len(seen)==1
