"""Built React in Chromium: approval recovery, research SSE and save retries."""
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('BACKTEST_BROWSER_URL'), reason='browser preview not requested')
USER, SESSION = 'agent-browser', 'research-session'
APPROVAL = 'lg_approve_' + 'a' * 64
RUN = '11111111-1111-4111-8111-111111111111'
STRATEGY = '22222222-2222-4222-8222-222222222222'


def task(ident, kind, status='queued'):
    return {'task_id':ident,'task_type':kind,'owner_id':USER,'session_id':SESSION,'status':status,
            'progress':1 if status=='succeeded' else .1,'message':'研究测试任务','created_at':'2026-11-18T08:00:00Z',
            'updated_at':'2026-11-18T08:00:01Z','started_at':'2026-11-18T08:00:01Z','finished_at':None,
            'timeout_seconds':3600,'max_retries':0,'attempt':0,'cancel_requested':False}


@pytest.fixture
def agent_browser(request):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        executable = os.getenv('CHROMIUM_EXECUTABLE_PATH')
        browser = pw.chromium.launch(headless=True, **({'executable_path':executable,'chromium_sandbox':True} if executable else {}))
        context = browser.new_context(viewport={'width':1440,'height':1100}, locale='zh-CN')
        page = context.new_page()
        page.set_default_timeout(10000)
        state = {'decision':'','resumed':False,'release':True,'acknowledged':False,'research':None,'save_fail':False,
                 'saved':False,'confirm_fail':False,'calls':[],'unexpected':[],'messages':[],'errors':[]}
        page.on('pageerror', lambda err: state['errors'].append(str(err)))

        def reply(route):
            req = route.request
            path = urlparse(req.url).path
            query = parse_qs(urlparse(req.url).query)
            body = req.post_data_json if req.post_data else None
            state['calls'].append((req.method,path,body))
            data, status = {}, 200
            if path == '/api/v1/auth/me':
                route.fulfill(json={'user':{'user_id':USER,'display_name':'研究测试','role':'user'}}); return
            if path == '/api/v1/health': data={'status':'ok','version':'test','deployment_mode':'test'}
            elif path == '/api/v1/web/agent/sessions':
                data={'records':[{'conversation_id':SESSION,'user_id':USER,'title':'策略研究测试','status':'active',
                    'language':'zh','created_at':'2026-11-18','updated_at':'2026-11-18','last_message_at':'','last_run_id':''}],'total':1}
            elif path.endswith('/messages'):
                data={'records':state['messages'],'total':len(state['messages'])}
            elif path.endswith('/strategy-proposal'): data={'available':False,'proposal':None,'versions':[]}
            elif path == '/api/v1/web/agent/pending-actions':
                records=[] if state['resumed'] else [{'proposal_id':APPROVAL,'plan_id':APPROVAL,'source':'langgraph',
                    'proposal_version':1,'payload_hash':'b'*64,'conversation_id':SESSION,'run_id':'run-fixture',
                    'intent':'run_strategy_backtest','operation_type':'run_strategy_backtest','confirmation_status':'pending',
                    'execution_status':'queued' if state['decision'] else 'pending','decision':state['decision'],
                    'task_id':'resume-test' if state['decision'] else '', 'can_retry':not state['decision'],
                    'created_at':'2026-11-18T08:00:00Z','expires_at':'2026-11-19T08:00:00Z',
                    'before_state_summary':{},'proposed_changes':{'name':'趋势策略','factors':['MA5'],'initial_cash':1000000},
                    'after_state_preview':{},'warnings':[],'validation_results':{},'confirmation_phrase':'CONFIRM-AAAAAA'}]
                data={'records':records,'total':len(records)}
            elif path.endswith('/confirm') or path.endswith('/reject'):
                if state['confirm_fail']:
                    state['confirm_fail']=False
                    route.fulfill(status=503,json={'detail':'请重试审批'}); return
                state['decision']='approved' if path.endswith('/confirm') else 'rejected'
                assert body['conversation_id']==SESSION
                if state['decision']=='approved': assert body['confirmation_text']=='CONFIRM-AAAAAA'
                data={'request_id':body['request_id'],'idempotency_key':body['idempotency_key'],'task':task('resume-test','agent.resume')}
            elif path == '/api/v1/tasks':
                kind=query.get('task_type',[''])[0]
                if kind=='backtest.run': data=[state['research']] if state['research'] else []
                elif kind=='agent.resume' and state['decision'] and not state['acknowledged']:
                    data=[task('resume-test','agent.resume','succeeded' if state['resumed'] else 'queued')]
                else: data=[]
            elif path == '/api/v1/tasks/resume-test':
                data=task('resume-test','agent.resume','succeeded' if state['resumed'] else 'queued')
            elif path == '/api/v1/tasks/resume-test/events':
                if state['release']:
                    state['resumed']=True
                    if state['decision']=='approved': state['research']=task('research-test','backtest.run','running')
                    done=task('resume-test','agent.resume','succeeded')
                    done['result']={'success':True,'run_id':'run-fixture','answer':'研究请求已提交' if state['decision']=='approved' else '已拒绝本次研究操作'}
                    stream='event: task-complete\ndata: '+json.dumps({'task':done})+'\n\n'
                else: stream='event: task-event\ndata: '+json.dumps({'task_id':'resume-test','sequence':1,'event_type':'started','data':{'message':'恢复审批中'},'created_at':'2026-11-18'})+'\n\n'
                route.fulfill(content_type='text/event-stream',body=stream); return
            elif path == '/api/v1/tasks/research-test/events':
                done=task('research-test','backtest.run','succeeded')
                done['result']={'run_id':RUN,'kind':'backtest'}
                state['research']=done
                route.fulfill(content_type='text/event-stream',body='event: task-complete\ndata: '+json.dumps({'task':done})+'\n\n'); return
            elif path.endswith('/finalize-task'):
                assert body['task_id']=='resume-test'
                data={'message_id':'assistant-completed','conversation_id':SESSION,'user_id':USER,'role':'assistant',
                    'content':'研究请求已提交' if state['decision']=='approved' else '已拒绝本次研究操作',
                    'run_id':'','created_at':'2026-11-18T08:00:02Z','language':'zh'}
                state['messages']=[data]
            elif path.endswith('/acknowledge'):
                state['acknowledged']=True
                data=task('resume-test','agent.resume','succeeded')
            elif path == '/api/v1/web/strategies/from-report':
                assert body['run_id']==RUN and body['confirmed'] is True
                if state['save_fail']:
                    state['save_fail']=False
                    route.fulfill(status=503,json={'detail':'请重试保存'}); return
                state['saved']=True
                data={'strategy_id':STRATEGY,'version':1,'latest_run_id':RUN,'spec':{'name':'趋势策略'}}
            else:
                state['unexpected'].append(path)
                status=404; data={'detail':'unexpected endpoint'}
            route.fulfill(status=status,json={'success':True,'data':data})

        page.route('**/api/v1/**',reply)
        page.goto(os.environ['BACKTEST_BROWSER_URL']+'/agent')
        page.get_by_test_id('agent-composer-input').wait_for()
        try:
            yield page,state
        finally:
            directory=Path('browser-results'); directory.mkdir(exist_ok=True)
            page.screenshot(path=str(directory/(request.node.name+'.png')),full_page=True)
            (directory/(request.node.name+'.html')).write_text(page.content(),encoding='utf-8')
            (directory/(request.node.name+'.json')).write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
            context.close(); browser.close()
        assert not state['errors'],state['errors']
        assert not state['unexpected'],state['unexpected']


def decide(page, confirm=True):
    page.get_by_role('tab',name=re.compile('待确认操作')).click()
    panel=page.get_by_role('tabpanel').filter(has=page.get_by_text(APPROVAL,exact=False))
    panel.get_by_role('button',name=re.compile('^确.?认$' if confirm else '^拒.?绝$')).click()
    dialog=page.get_by_role('dialog')
    if confirm: dialog.get_by_placeholder('CONFIRM-AAAAAA').fill('CONFIRM-AAAAAA')
    dialog.get_by_role('button',name=re.compile('^确认执行$' if confirm else '^拒.?绝$')).click()


def test_agent_approval_research_sse_and_idempotent_save_retry(agent_browser):
    from playwright.sync_api import expect
    page,state=agent_browser
    assert not any(path.endswith('/confirm') for _,path,_ in state['calls'])
    decide(page)
    research=page.get_by_test_id('agent-research-tasks')
    expect(research.get_by_role('link',name='查看研究报告')).to_have_attribute('href',f'/strategies?report={RUN}')
    expect(page.get_by_test_id('agent-composer-input')).to_be_enabled()
    state['save_fail']=True
    research.get_by_role('button',name='保存策略',exact=True).click()
    page.get_by_role('dialog').get_by_role('button',name=re.compile('确.?定')).click()
    expect(page.get_by_text('请重试保存',exact=True)).to_be_visible()
    page.get_by_role('dialog').get_by_role('button',name=re.compile('确.?定')).click()
    expect(page.get_by_text('已从完成报告保存策略',exact=True)).to_be_visible()
    saves=[body for _,path,body in state['calls'] if path=='/api/v1/web/strategies/from-report']
    assert len(saves)==2 and saves[0]['request_id']==saves[1]['request_id']
    assert state['saved']


def test_agent_reject_never_creates_a_research_task(agent_browser):
    from playwright.sync_api import expect
    page,state=agent_browser
    decide(page,confirm=False)
    expect(page.get_by_text('已拒绝本次研究操作',exact=True)).to_be_visible()
    expect(page.get_by_test_id('agent-composer-input')).to_be_enabled()
    assert state['research'] is None
    assert not any(path.endswith('/confirm') for _,path,_ in state['calls'])


def test_agent_resume_recovery_after_refresh_does_not_resubmit_approval(agent_browser):
    from playwright.sync_api import expect
    page,state=agent_browser
    state['release']=False
    decide(page)
    expect(page.get_by_test_id('agent-composer-input')).to_be_disabled()
    state['release']=True
    page.reload()
    expect(page.get_by_test_id('agent-research-tasks').get_by_role('link',name='查看研究报告')).to_be_visible()
    assert len([1 for _,path,_ in state['calls'] if path.endswith('/confirm')])==1
    assert state['resumed']


def test_agent_confirmation_error_preserves_input_and_same_request(agent_browser):
    from playwright.sync_api import expect
    page,state=agent_browser
    state['confirm_fail']=True
    decide(page)
    expect(page.get_by_text('请重试审批',exact=True)).to_be_visible()
    dialog=page.get_by_role('dialog')
    expect(dialog.get_by_placeholder('CONFIRM-AAAAAA')).to_have_value('CONFIRM-AAAAAA')
    dialog.get_by_role('button',name=re.compile('确认执行')).click()
    expect(page.get_by_test_id('agent-research-tasks').get_by_role('link',name='查看研究报告')).to_be_visible()
    controls=[body for _,path,body in state['calls'] if path.endswith('/confirm')]
    assert len(controls)==2 and controls[0]['request_id']==controls[1]['request_id']
    assert controls[0]['idempotency_key']==controls[1]['idempotency_key']

