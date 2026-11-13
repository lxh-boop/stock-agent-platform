"""真实 PostgreSQL，严格限定临时测试库：快照、用户隔离、幂等与报告保留。"""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from core.user_scope import user_scope, user_schema
from core.alpha158_contract import feature_version
from database.alpha158_store import Alpha158Store
from database.connection import get_connection
from backtesting.contracts import BacktestSpec, AnalysisSpec, ScreenSpec
from backtesting import store
from application.backtest_service import BacktestService

pytestmark=pytest.mark.skipif(os.environ.get('STOCK_TEST_DATABASE')!='1',reason='temporary DB only')
USERS=('backtest_alice','backtest_bob')


@pytest.fixture(scope='module',autouse=True)
def schema(isolated_postgres_schema):
    yield


@pytest.fixture(autouse=True)
def prepare(schema):
    from psycopg import sql
    for user in USERS:
        with user_scope(user):
            Alpha158Store().read()
            with get_connection() as conn:
                table,state=Alpha158Store._ensure(conn)
                report=store._reports(conn)
                strategy=store._strategies(conn)
                conn.execute(sql.SQL('TRUNCATE {}').format(strategy))
                conn.execute(sql.SQL('TRUNCATE {},{},{}').format(table,state,report))
                for i in range(5,9):
                    # 一致原始行情与复权因子；只有所选KMID列有值。
                    payload=json.dumps({'open':10.,'close':float(5+i),'volume':1000.,'adj_factor':1.})
                    conn.execute(sql.SQL('''INSERT INTO {} (feature_version,trade_date,stock_code,input_hash,source_payload,"KMID")
                        VALUES (%s,%s,'000001',%s,%s,1)''').format(table),
                        (feature_version(),f'2026-01-{i:02}',str(i),payload))
                conn.execute(sql.SQL('''INSERT INTO {} (feature_version,latest_signal_date,retention_cutoff,feature_names)
                    VALUES (%s,'2026-01-08','2021-01-08','[]')''').format(state),(feature_version(),))
                conn.commit()


def params():
    return {'start_date':'2026-01-05','end_date':'2026-01-08','factors':[{'field':'KMID'}],
            'initial_cash':10000,'buy_fee':0,'sell_fee':0,'max_positions':1}


def test_real_query_roundtrip_and_no_changes_to_account():
    with user_scope(USERS[0]):
        with get_connection(scoped=False) as conn:
            before=conn.execute('SELECT * FROM paper_account ORDER BY account_id').fetchall()
        report=BacktestService.run(params())
        value=store.get_report(report['run_id'])
        assert value['metrics']['final_equity']==13000
        assert len(value['trades'])==1 and len(value['equity'])==4
        assert value['data_snapshot']['isolation']=='repeatable_read'
        with get_connection(scoped=False) as conn:
            assert conn.execute('SELECT * FROM paper_account ORDER BY account_id').fetchall()==before


def test_cross_user_reports_not_accessible_and_missing_scope_refused():
    with user_scope(USERS[0]):
        ident=BacktestService.run(params())['run_id']
    with user_scope(USERS[1]):
        assert store.list_reports()['total']==0
        with pytest.raises(KeyError): store.get_report(ident)
    with pytest.raises(ValueError): store.catalog()


def test_snapshot_never_changes_when_source_updates_during_read():
    from psycopg import sql
    with user_scope(USERS[0]):
        with store.snapshot(BacktestSpec.model_validate(params())) as (meta,iterator):
            with get_connection() as conn:
                conn.execute(sql.SQL('UPDATE {} SET "KMID"=999').format(sql.Identifier(user_schema(USERS[0]),'alpha158_daily')))
                conn.commit()
            assert all(row['KMID']==1 for _,bars in iterator for row in bars.values())
        with store.snapshot(BacktestSpec.model_validate(params())) as (_,iterator):
            assert all(row['KMID']==999 for _,bars in iterator for row in bars.values())


def test_report_survives_factor_retention_and_bad_data_creates_no_report():
    from psycopg import sql
    with user_scope(USERS[0]):
        ident=BacktestService.run(params())['run_id']
        with get_connection() as conn:
            conn.execute(sql.SQL('DELETE FROM {}').format(sql.Identifier(user_schema(USERS[0]),'alpha158_daily')))
            conn.commit()
        assert store.get_report(ident)['trades']
        with pytest.raises(ValueError): BacktestService.run(params())
        assert store.list_reports()['total']==1


def test_strict_range_unknown_stocks_and_same_id_cannot_overwrite_report():
    with user_scope(USERS[0]):
        with pytest.raises(ValueError,match='超出'): BacktestService.run({**params(),'start_date':'2020-01-01'})
        with pytest.raises(ValueError,match='部分'): BacktestService.run({**params(),'stock_codes':['000001','000002']})
        ident=BacktestService.run(params())['run_id']
        original=store.get_report(ident)
        store.save_report(ident,{'bad':'replacement'})
        assert store.get_report(ident)==original


def test_durable_submission_idempotence_and_one_active_per_user(monkeypatch):
    from server.api import tasks as api
    from server.task_runtime.store import TaskStore
    from psycopg import sql
    with get_connection(runtime=True) as conn:
        conn.execute('DELETE FROM task_events WHERE task_id IN (SELECT task_id FROM task_runs WHERE owner_id=ANY(%s))',(list(USERS),))
        conn.execute('DELETE FROM task_runs WHERE owner_id=ANY(%s)',(list(USERS),));conn.commit()
    class FakeManager:
        def __init__(self): self.store=TaskStore();self.count=0
        def submit(self,**kw):
            self.count+=1
            return self.store.create(task_id='task_'+uuid4().hex, task_type=kw['task_type'],owner_id=kw['owner_id'],
                request={'args':[],'kwargs':kw['kwargs']},metadata=kw['metadata'])
    fake=FakeManager();monkeypatch.setattr(api,'task_manager',fake)
    req=str(uuid4())
    with user_scope(USERS[0]):
        a=BacktestService.submit(params(),request_id=req)
        assert a['task_id']==BacktestService.submit(params(),request_id=req)['task_id']
        with pytest.raises(ValueError,match='不同'): BacktestService.submit({**params(),'name':'changed'},request_id=req)
        with pytest.raises(ValueError,match='已有'): BacktestService.submit(params(),request_id=str(uuid4()))
        assert fake.count==1
    with user_scope(USERS[1]):
        assert BacktestService.submit(params(),request_id=req)['task_id']!=a['task_id']



def test_real_paging_summary_export_and_research_reports():
    with user_scope(USERS[0]):
        result=BacktestService.run(params())
        ident=result['run_id']
        assert 'positions' not in store.get_report(ident,summary=True)
        assert len(store.get_report(ident)['positions'])==3
        page=store.report_rows(ident,'positions',1,1)
        assert len(page['records'])==1 and page['total']==3 and page['records'][0]['date']=='2026-01-07'
        analysis={'start_date':'2026-01-05','end_date':'2026-01-08','factors':[{'field':'KMID'}],'interval_days':1}
        a=BacktestService.run(analysis,kind='analysis')
        assert a['kind']=='analysis' and store.list_reports(kind='analysis')['total']==1
        rows=store.report_rows(a['run_id'],'ic_series',50,0,'KMID')
        assert rows['total']==3 and all(r['ic'] is None for r in rows['records'])
        assert store.report_rows(a['run_id'],'ic_series',50,0,'MA5')['total']==0
        sample=BacktestService.run({**params(),'start_date':'2026-01-08'},kind='screen')
        assert store.get_report(sample['run_id'])['selections'][0]['code']=='000001'
        assert store.list_reports()['total']==3
        store.delete_report(ident)
        with pytest.raises(KeyError):store.get_report(ident)


def test_saved_strategy_and_execution_lock_are_account_scoped():
    ident=str(uuid4())
    with user_scope(USERS[0]):
        store.save_strategy(ident,params())
        with store.execution_lock():
            with pytest.raises(ValueError,match='正在计算'):
                with store.execution_lock():pass
            with user_scope(USERS[1]):
                with store.execution_lock():pass
                assert store.list_strategies()['total']==0
                with pytest.raises(KeyError):store.delete_strategy(ident)
        store.save_strategy(ident,{**params(),'name':'新版策略'})
        assert store.list_strategies()['total']==1
        assert store.list_strategies()['records'][0]['spec']['name']=='新版策略'
        store.delete_strategy(ident)
        assert store.list_strategies()['total']==0


def test_completed_request_returns_same_report_after_data_changes():
    from psycopg import sql
    ident=str(uuid4())
    with user_scope(USERS[0]):
        original=BacktestService.run(params(),run_id=ident)
        with get_connection() as conn:
            conn.execute(sql.SQL('DELETE FROM {}').format(sql.Identifier(user_schema(USERS[0]),'alpha158_daily')));conn.commit()
        restored=BacktestService.run(params(),run_id=ident)
        assert restored['data_hash']==original['data_hash']
        with pytest.raises(ValueError,match='不同参数'):BacktestService.run({**params(),'name':'different'},run_id=ident)


def test_real_personal_index_cache_and_market_snapshot():
    import pandas as pd
    from backtesting.market import prepare
    class Client:
        def trade_cal(self,**kw):
            days=pd.bdate_range(kw['start_date'],kw['end_date'])
            return pd.DataFrame({'cal_date':days.strftime('%Y%m%d'),'is_open':'1'})
        def index_daily(self,**kw):
            days=pd.bdate_range(kw['start_date'],kw['end_date'])
            return pd.DataFrame({'trade_date':days.strftime('%Y%m%d'),'open':100.,'high':101.,'low':99.,'close':100.,'pre_close':100.,'vol':1000.,'amount':10000.})
    p=BacktestSpec.model_validate({**params(),'benchmark':'hs300'})
    with user_scope(USERS[0]):
        prepare(p,client=Client())
        with store.snapshot(p) as (meta,days):
            assert meta['index_sources']['hs300']['source']=='personal_tushare/index_daily'
            assert len(meta['_markets'])>200
            assert list(days)
    with user_scope(USERS[1]):
        with get_connection() as conn:
            assert conn.execute('SELECT to_regclass(%s) AS t',(user_schema(USERS[1])+'.backtest_index_cache',)).fetchone()['t'] is None
