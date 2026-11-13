"""真实 Chromium + 构建后的前端；API 用本地确定性夹具，不访问生产或真实 Token。
运行前先启动 Vite preview；需环境 BACKTEST_BROWSER_URL，未设置则跳过。
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse, parse_qs
from uuid import UUID

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('BACKTEST_BROWSER_URL'), reason='Browser preview not requested')
ROOT = '/api/v1/web/backtests'
IDENT = '11111111-1111-4111-8111-111111111111'
SPEC = {'name':'浏览器回测报告','start_date':'2026-01-05','end_date':'2026-02-05',
    'factors':[{'field':'ROC20','direction':-1,'weight':1}], 'stock_codes':None,
    'initial_cash':1000000,'max_positions':10,'rebalance_days':5,'exposure':1,
    'execution':'open','buy_fee':.0003,'sell_fee':.0008,'slippage_bps':0,
    'weighting':{'method':'rank_ic','window':20},'sizing':{'method':'factor','field':'MA5','proportions':[]},
    'market_filters':[],'entry_filters':[],'keep_rules':[],'sell_filters':[], 'benchmark':'hs300',
    'timing':{'rules':[{'index':'sse','field':'close_ma20','op':'gt','value':1}], 'enter_votes':1,'exit_votes':1,'bear_exposure':.2}}
REPORT = {'run_id':IDENT,'kind':'backtest','created_at':'2026-02-06T00:00:00Z','spec':SPEC,
    'engine_version':'factor-event-v2','data_hash':'browser-fixture','data_snapshot':{},'diagnostics':{},
    'metrics':{'total_return':.1,'annual_return':.2,'max_drawdown':-.05,'sharpe':1.2,'trade_count':120,'total_fees':120},
    'assumptions':['浏览器交互测试夹具，不是实际投资业绩。'],'disclaimer':'研究测试'}


@pytest.fixture()
def browser_page(request):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1440,'height':1100}, locale='zh-CN')
        page = context.new_page()
        calls, errors, strategies = [], [], []
        page.on('pageerror', lambda err: errors.append(str(err)))

        def reply(route):
            request=route.request
            path=urlparse(request.url).path
            query=parse_qs(urlparse(request.url).query)
            method=request.method
            body=request.post_data_json if method in {'POST','PUT','DELETE'} and request.post_data else None
            calls.append((method,path,body,query))
            status=200
            if path=='/api/v1/auth/me':
                route.fulfill(json={'user':{'user_id':'browser-alice','display_name':'测试账号','role':'user'}});return
            if path=='/api/v1/health':data={'status':'ok','version':'test','deployment_mode':'test'}
            elif path==ROOT+'/catalog': data={'fields':['ROC20','MA5','KMID','STD20'],
                'index_fields':['close','pct_chg','close_ma20'],'indexes':{'sse':'000001.SH'},'schemas':{},
                'max_stocks':1000,'max_positions':50,'message':'','coverage':{
                    'start_date':'2026-01-05','end_date':'2026-02-05','stock_count':10,'feature_version':'test'}}
            elif path=='/api/v1/tasks':data=[]
            elif path==ROOT+'/strategies':
                if method=='PUT':
                    strategies[:]=[{'strategy_id':body['strategy_id'],'spec':body['spec'],'updated_at':'2026-02-06'}]
                    data=strategies[0]
                else:data={'records':strategies,'total':len(strategies)}
            elif path.startswith(ROOT+'/strategies/') and method=='DELETE':strategies.clear();data={'deleted':True}
            elif path in {ROOT,ROOT+'/analyses',ROOT+'/screen'} and method=='POST':
                data={'task_id':'browser-task','task_type':'backtest.run','owner_id':'browser-alice',
                    'status':'failed','progress':0,'message':'测试夹具：只检查提交参数','error':{'message':'fixture'},
                    'session_id':'','created_at':'2026-02-06','updated_at':'2026-02-06','timeout_seconds':3600,
                    'max_retries':0,'attempt':0,'cancel_requested':False}
            elif path==ROOT:data={'records':[REPORT],'total':1}
            elif path==ROOT+'/'+IDENT:data=REPORT
            elif path==ROOT+'/'+IDENT+'/rows':
                kind=query.get('kind',[''])[0]
                if kind=='equity': rows=[{'date':'2026-01-05','nav':1,'benchmark_nav':1},{'date':'2026-02-05','nav':1.1,'benchmark_nav':1.05}]
                elif kind=='trades':rows=[{'date':'2026-01-06','code':'000001','side':'buy','shares':100,'price':10,'amount':1000,'fee':.3,'reason':f'成交{i}'} for i in range(120)]
                elif kind=='positions':rows=[{'date':'2026-01-06','code':'000001','shares':100,'weight':.1,'close':10,'stale':False}]
                elif kind=='signals':rows=[{'date':'2026-01-05','weights':{'ROC20':1},'next_exposure':.2}]
                else:rows=[]
                limit=int(query.get('limit',['50'])[0]);offset=int(query.get('offset',['0'])[0])
                data={'records':rows[offset:offset+limit],'total':len(rows),'limit':limit,'offset':offset}
            elif path==ROOT+'/'+IDENT+'/export':
                route.fulfill(content_type='application/json',body=json.dumps({**REPORT,'trades':[{'n':i} for i in range(120)]}));return
            elif path.startswith('/api/v1/tasks/'):data={'task_id':'browser-task','status':'failed','progress':0,'message':'测试'}
            else: status=404;data={'detail':'unexpected test endpoint '+path}
            route.fulfill(status=status,json={'success':True,'data':data,'request_id':'browser-fixture'})

        page.route('**/api/v1/**',reply)
        page.goto(os.environ['BACKTEST_BROWSER_URL']+'/backtests')
        page.get_by_role('heading',name='策略研究',exact=True).wait_for()
        try:
            yield page,calls,strategies
        finally:
            result=Path('browser-results')
            result.mkdir(exist_ok=True)
            prefix=result / request.node.name
            page.screenshot(path=str(prefix)+'.png',full_page=True)
            Path(str(prefix)+'.html').write_text(page.content(),encoding='utf-8')
            Path(str(prefix)+'.json').write_text(json.dumps({'buttons':page.get_by_role('button').all_text_contents(),'body':page.locator('body').inner_text(),'errors':errors,'calls':calls},ensure_ascii=False,indent=2),encoding='utf-8')
            context.close();browser.close()
        assert not errors,errors


def confirm(page):
    page.get_by_role('dialog').get_by_role('button',name=re.compile('确.?定')).click()


def test_all_study_modes_and_advanced_parameters(browser_page):
    page,calls,_=browser_page
    page.get_by_role('button',name='导入 / 查看 JSON 参数').click()
    dialog=page.get_by_role('dialog')
    dialog.locator('textarea').fill(json.dumps(SPEC,ensure_ascii=False))
    dialog.get_by_role('button',name='应用到表单').click()
    page.get_by_role('button',name='运行策略回测',exact=True).click();confirm(page)
    page.wait_for_function("document.body.innerText.includes('已提交本人研究任务')")
    request=next(c[2] for c in calls if c[0]=='POST' and c[1]==ROOT)
    UUID(request['request_id']);assert request['confirmed'] is True
    assert request['spec']['weighting']['method']=='rank_ic'
    assert request['spec']['sizing']['field']=='MA5'
    assert request['spec']['timing']['bear_exposure']==.2
    assert 'user_id' not in request and 'owner_id' not in request
    from playwright.sync_api import expect
    analysis_tab=page.get_by_role('tab',name='因子分析',exact=True)
    analysis_tab.click()
    expect(analysis_tab).to_have_attribute('aria-selected','true')
    page.get_by_role('button',name='运行因子分析',exact=True).click();confirm(page)
    page.wait_for_timeout(350)
    request=next(c[2] for c in calls if c[0]=='POST' and c[1]==ROOT+'/analyses')
    assert 'initial_cash' not in request['spec'] and request['spec']['segment_count']==5
    screen_tab=page.get_by_role('tab',name='每日选股',exact=True)
    screen_tab.click()
    expect(screen_tab).to_have_attribute('aria-selected','true')
    page.get_by_role('button',name='运行每日选股',exact=True).click();confirm(page)
    page.wait_for_timeout(350)
    request=next(c[2] for c in calls if c[0]=='POST' and c[1]==ROOT+'/screen')
    assert request['spec']['start_date']==request['spec']['end_date']
    assert request['spec']['weighting']==SPEC['weighting']
    assert request['spec']['sizing']==SPEC['sizing']
    assert request['spec']['timing']==SPEC['timing']
    Path('browser-results').mkdir(exist_ok=True)
    page.screenshot(path='browser-results/research-screen.png',full_page=True)


def test_strategy_save_edit_report_paging_and_export(browser_page,tmp_path):
    page,calls,strategies=browser_page
    # 高级折叠面板始终保持关闭，验证保存和再次编辑不能丢失导入参数。
    page.get_by_role('button',name='导入 / 查看 JSON 参数').click()
    dialog=page.get_by_role('dialog')
    dialog.locator('textarea').fill(json.dumps(SPEC,ensure_ascii=False))
    dialog.get_by_role('button',name='应用到表单').click()
    page.get_by_role('button',name='另存为新策略').click();confirm(page)
    page.wait_for_timeout(350)
    assert len(strategies)==1
    assert strategies[0]['spec']['weighting']==SPEC['weighting']
    assert strategies[0]['spec']['sizing']==SPEC['sizing']
    assert strategies[0]['spec']['timing']==SPEC['timing']
    page.get_by_role('button',name='编辑 / 重跑').click()
    page.get_by_label('名称',exact=True).fill('编辑后的个人策略')
    page.get_by_role('button',name='保存修改',exact=True).click();confirm(page)
    page.wait_for_timeout(350)
    assert strategies[0]['spec']['name']=='编辑后的个人策略'
    assert strategies[0]['spec']['weighting']==SPEC['weighting']
    assert strategies[0]['spec']['sizing']==SPEC['sizing']
    assert strategies[0]['spec']['timing']==SPEC['timing']
    # 完整报告页而非旧latest，分页取第2页，不只保留最近50笔。
    page.get_by_role('tab',name='全部成交',exact=True).click()
    page.locator('.ant-pagination-item-2').last.click()
    page.wait_for_timeout(300)
    assert any(c[1].endswith('/rows') and c[3].get('kind')==['trades'] and c[3].get('offset')==['50'] for c in calls)
    page.get_by_role('tab',name='逐日持仓',exact=True).click()
    with page.expect_download() as info:page.get_by_role('button',name='导出完整JSON报告').click()
    download=info.value;path=tmp_path/'report.json';download.save_as(path)
    assert len(json.loads(path.read_text())['trades'])==120
    Path('browser-results').mkdir(exist_ok=True)
    page.screenshot(path='browser-results/research-report.png',full_page=True)
