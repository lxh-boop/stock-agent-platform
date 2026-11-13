from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import date
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from core.user_scope import current_owner, user_scope
from application.backtest_service import BacktestService
from backtesting import store
from server.api.security import AuthUser, require_user
from server.api.routers.web_backtests import router


def params():
    return {'start_date':'2026-01-05', 'end_date':'2026-01-07', 'factors':[{'field':'KMID'}],
            'buy_fee':0,'sell_fee':0,'initial_cash':10000,'max_positions':1}


@pytest.fixture
def client():
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[require_user]=lambda:AuthUser('alice','Alice','user')
    return TestClient(app)


def test_anonymous_cannot_read_catalog():
    app=FastAPI();app.include_router(router)
    assert TestClient(app).get('/api/v1/web/backtests/catalog').status_code==401


def test_normal_user_submit_injects_scope_and_requires_confirmation(client,monkeypatch):
    calls=[]
    def submit(spec,request_id,kind='backtest'):
        calls.append((current_owner(),spec,request_id))
        return {'task_id':'t','status':'queued'}
    monkeypatch.setattr(BacktestService,'submit',submit)
    body={'spec':params(),'request_id':str(uuid4()),'confirmed':True}
    assert client.post('/api/v1/web/backtests',json=body).status_code==200
    assert calls[0][0]=='alice'
    body['confirmed']=False
    assert client.post('/api/v1/web/backtests',json=body).status_code==422
    body['confirmed']=True;body['user_id']='bob'
    assert client.post('/api/v1/web/backtests',json=body).status_code==422
    assert len(calls)==1 and not current_owner()


def test_catalog_uses_current_identity(client,monkeypatch):
    monkeypatch.setattr(store,'catalog',lambda:{'fields':[],'owner':current_owner()})
    assert client.get('/api/v1/web/backtests/catalog').json()['data']['owner']=='alice'


def test_old_latest_endpoint_and_unavailable_report(client,monkeypatch):
    assert client.get('/api/v1/web/backtests/latest').status_code==422
    def missing(_, **kwargs): raise KeyError('not your report')
    monkeypatch.setattr(store,'get_report',missing)
    assert client.get('/api/v1/web/backtests/'+str(uuid4())).status_code==404


def test_report_summary_excludes_big_arrays_and_paging(client,monkeypatch):
    ident=str(uuid4())
    def get_report(_, summary=False):
        assert summary is True
        return {'run_id':ident,'metrics':{}}
    monkeypatch.setattr(store,'get_report',get_report)
    def page(_,kind,limit,offset,factor=None):
        assert kind=='trades' and limit==50 and offset==50 and factor is None
        return {'records':[{'i':i} for i in range(offset,offset+limit)],'total':150}
    monkeypatch.setattr(store,'report_rows',page)
    assert 'trades' not in client.get('/api/v1/web/backtests/'+ident).json()['data']
    result=client.get(f'/api/v1/web/backtests/{ident}/rows?kind=trades&offset=50&limit=50').json()['data']
    assert result['records'][0]['i']==50 and result['total']==150
    assert client.get(f'/api/v1/web/backtests/{ident}/rows?kind=unknown').status_code==422


def test_errors_have_useful_user_message_but_do_not_leak_database(client,monkeypatch):
    def invalid(): raise ValueError('个人数据不完整')
    monkeypatch.setattr(store,'catalog',invalid)
    assert client.get('/api/v1/web/backtests/catalog').json()['detail']=='个人数据不完整'
    def internal(): raise RuntimeError('postgresql://user:secret@host')
    monkeypatch.setattr(store,'catalog',internal)
    response=client.get('/api/v1/web/backtests/catalog')
    assert response.status_code==503 and 'secret' not in response.text


def test_application_real_engine_saves_only_completed_personal_report(monkeypatch):
    bars=[(date(2026,1,d),{'000001':{'open':10.,'close':10.+d-5,'volume':1000.,'adj_factor':1.,'KMID':1.}}) for d in (5,6,7)]
    @contextmanager
    def snapshot(_):
        assert current_owner()=='alice'
        yield {'feature_version':'test'},iter(deepcopy(bars))
    saved=[]
    monkeypatch.setattr(store,'snapshot',snapshot)
    monkeypatch.setattr(store,'execution_lock',nullcontext)
    monkeypatch.setattr(store,'save_report',lambda id,report:saved.append((current_owner(),id,report)))
    with pytest.raises(ValueError,match='登录'): BacktestService.run(params())
    with user_scope('alice'):
        summary=BacktestService.run(params())
        assert summary['metrics']['final_equity']==12000
        assert 'trades' not in summary
        assert saved[0][0]=='alice' and saved[0][2]['trades']
        with pytest.raises(InterruptedError): BacktestService.run(params(),cancelled=lambda:True)
        assert len(saved)==1


def test_no_old_fixed_return_backtester_is_left():
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    assert not (root/'agent_graph/services/strategy_backtest_service.py').exists()
    assert not (root/'server/api/presenters/backtests.py').exists()
    source=(root/'agent_graph/services/strategy_review_service.py').read_text()
    assert 'self.backtester' not in source
    assert '"status": "blocked"' in source


@pytest.mark.parametrize('path,kind', [('analyses','analysis'),('screen','screen')])
def test_research_endpoints_inject_user_scope(client,monkeypatch,path,kind):
    calls=[]
    def submit(spec,request_id,kind):
        calls.append((current_owner(),kind,spec))
        return {'task_id':'t','status':'queued'}
    monkeypatch.setattr(BacktestService,'submit',submit)
    body={'spec':{'start_date':'2026-01-05','end_date':'2026-01-07','factors':[{'field':'KMID'}]},
          'request_id':str(uuid4()),'confirmed':True}
    if kind=='screen':body['spec']['start_date']=body['spec']['end_date']
    assert client.post('/api/v1/web/backtests/'+path,json=body).status_code==200
    assert calls[0][:2]==('alice',kind)
    body['spec']['owner_id']='bob'
    assert client.post('/api/v1/web/backtests/'+path,json=body).status_code==422


def test_strategy_save_delete_and_report_delete_require_confirmation(client,monkeypatch):
    calls=[]
    monkeypatch.setattr(store,'save_strategy',lambda id,spec:calls.append((current_owner(),id)) or {'strategy_id':id})
    monkeypatch.setattr(store,'delete_strategy',lambda id:calls.append((current_owner(),id)))
    monkeypatch.setattr(store,'delete_report',lambda id:calls.append((current_owner(),id)))
    ident=str(uuid4())
    assert client.put('/api/v1/web/backtests/strategies',json={'spec':params(),'strategy_id':ident,'confirmed':True}).status_code==200
    for path in ('/strategies/'+ident,'/'+ident):
        assert client.request('DELETE','/api/v1/web/backtests'+path,json={'confirmed':False}).status_code==422
        assert client.request('DELETE','/api/v1/web/backtests'+path,json={'confirmed':True}).status_code==200
    assert calls==[('alice',ident)]*3


def test_export_is_owned_and_includes_full_report(client,monkeypatch):
    def report(id,**kwargs):
        assert current_owner()=='alice'
        return {'run_id':id,'trades':[{'i':i} for i in range(200)]}
    monkeypatch.setattr(store,'get_report',report)
    response=client.get('/api/v1/web/backtests/'+str(uuid4())+'/export')
    assert response.status_code==200 and len(response.json()['trades'])==200
    assert response.headers['content-disposition'].startswith('attachment;')
