"""Built React in real Chromium, deterministic API fixtures, no production writes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest

from test_strategy_research_browser import IDENT, REPORT, SPEC

pytestmark = pytest.mark.skipif(not os.getenv('BACKTEST_BROWSER_URL'), reason='Browser preview not requested')
ROOT = '/api/v1/web/strategies'
BACKTEST = '/api/v1/web/backtests'
STRATEGY_ID = '22222222-2222-4222-8222-222222222222'
OWNER = 'browser-alice'


@pytest.fixture
def workspace_page(request):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        launch = {'headless': True}
        if os.environ.get('CHROMIUM_EXECUTABLE'):
            launch.update(executable_path=os.environ['CHROMIUM_EXECUTABLE'], chromium_sandbox=True)
        browser = pw.chromium.launch(**launch)
        context = browser.new_context(viewport={'width': 1440, 'height': 1050}, locale='zh-CN')
        page = context.new_page()
        calls, errors = [], []
        report = deepcopy(REPORT)
        report['metrics']['sell_win_rate'] = .75
        equity = [{'date': '2026-01-05', 'nav': 1., 'benchmark_nav': 1.},
                  {'date': '2026-02-05', 'nav': 1.1, 'benchmark_nav': 1.05}]
        strategy = {'strategy_id': STRATEGY_ID, 'spec': deepcopy(SPEC), 'version': 2,
            'status': 'draft', 'created_at': '2026-02-06T00:00:00Z', 'updated_at': '2026-02-07T00:00:00Z',
            'latest_run_id': IDENT, 'latest_run_version': 1, 'latest_report': {**report, 'equity': equity},
            'active_task_id': None, 'last_task_id': None, 'task': None, 'error': None}
        state = {'records': [strategy], 'task': None, 'fail_next_save': False, 'save_requests': [], 'fail_next_write': None}
        page.on('pageerror', lambda value: errors.append(str(value)))

        def reply(route):
            req = route.request
            path, query = urlparse(req.url).path, parse_qs(urlparse(req.url).query)
            method = req.method
            body = req.post_data_json if req.post_data else None
            calls.append((method, path, body, query))
            failure = state['fail_next_write']
            if failure and method == failure['method'] and path.startswith(failure['prefix']):
                state['fail_next_write'] = None
                route.fulfill(status=503, json={'detail': '测试：操作暂时不可用，请重试'})
                return
            if state['task'] and state['task']['status'] == 'failed':
                strategy.update(status='failed', active_task_id=None, error='测试：行情缺口，旧报告保留')
            status = 200
            if path == '/api/v1/auth/me':
                route.fulfill(json={'user': {'user_id': OWNER, 'display_name': '测试账号', 'role': 'user'}})
                return
            if path == '/api/v1/health':
                data = {'status': 'ok', 'version': 'test'}
            elif path == BACKTEST + '/catalog':
                data = {'fields': ['ROC20', 'MA5', 'KMID'], 'indexes': {}, 'index_fields': [], 'schemas': {},
                    'max_stocks': 1000, 'max_positions': 50, 'message': '',
                    'coverage': {'start_date': '2026-01-05', 'end_date': '2026-02-05', 'stock_count': 300, 'feature_version': 'test'}}
            elif path == ROOT:
                # Aggregate intentionally differs from this page's count to catch
                # clients accidentally recomputing statistics from visible cards.
                data = {'records': state['records'], 'total': len(state['records']),
                    'stats': {'total': 42, 'running': int(bool(state['task']) and state['task']['status'] == 'running'),
                              'draft': 1, 'failed': 0, 'succeeded': 41, 'avg_annual_return': .15, 'avg_sharpe': 1.25}}
            elif path == ROOT + '/from-report':
                UUID(body['request_id'])
                assert body['confirmed'] and body['run_id'] == IDENT
                assert 'spec' not in body and 'owner_id' not in body
                state['save_requests'].append(deepcopy(body))
                if state['fail_next_save']:
                    state['fail_next_save'] = False
                    route.fulfill(status=503, json={'detail': '测试：保存暂时不可用，请重试'})
                    return
                saved = {**deepcopy(strategy), 'strategy_id': body['request_id'], 'version': 1,
                         'latest_run_version': 1, 'status': 'succeeded'}
                saved['spec']['name'] = body.get('name') or report['spec']['name']
                state['records'].append(saved)
                data = saved
            elif path == ROOT + '/' + STRATEGY_ID + '/rerun':
                UUID(body['request_id'])
                assert body['expected_version'] == 2 and body['confirmed']
                state['task'] = {'task_id': 'task-workspace-fixture', 'task_type': 'backtest.run', 'owner_id': OWNER,
                    'session_id': '', 'status': 'running', 'progress': .25, 'message': '正在计算真实数据范围',
                    'created_at': '2026-02-08T00:00:00Z', 'updated_at': '2026-02-08T00:00:00Z',
                    'started_at': '2026-02-08T00:00:00Z', 'finished_at': None, 'timeout_seconds': 3600,
                    'max_retries': 0, 'attempt': 0, 'cancel_requested': False}
                strategy.update(status='running', active_task_id=state['task']['task_id'],
                                last_task_id=state['task']['task_id'], task=state['task'])
                data = state['task']
            elif path.startswith(ROOT + '/') and (method == 'PUT' or any(item['strategy_id']==path.rsplit('/',1)[-1] for item in state['records'])):
                selected_strategy=next((item for item in state['records'] if item['strategy_id']==path.rsplit('/',1)[-1]), None)
                if method == 'DELETE':
                    assert body['expected_version'] == strategy['version'] and body['confirmed']
                    state['records'].clear()
                    data = {'deleted': True}
                elif method == 'PUT':
                    assert body['confirmed']
                    if selected_strategy:
                        assert body['expected_version'] == selected_strategy['version']
                        selected_strategy.update(spec=body['spec'], version=selected_strategy['version'] + 1)
                    else:
                        selected_strategy = {**deepcopy(strategy), 'strategy_id': path.rsplit('/', 1)[-1],
                            'spec': body['spec'], 'version': 1, 'status': 'draft', 'latest_run_id': None,
                            'latest_run_version': None, 'latest_report': None, 'active_task_id': None, 'last_task_id': None, 'task': None}
                        state['records'].append(selected_strategy)
                    data = selected_strategy
                else:
                    if state['task'] and state['task']['status'] == 'failed':
                        strategy.update(status='failed', active_task_id=None, error='测试：行情缺口，旧报告保留')
                    data = selected_strategy
            elif path == BACKTEST:
                data = {'records': [report], 'total': 1} if method == 'GET' else {
                    'task_id': 'task-research-submit-fixture', 'task_type': 'backtest.run', 'owner_id': OWNER,
                    'session_id': '', 'status': 'failed', 'progress': 1, 'message': '测试：只验证提交',
                    'created_at': '2026-02-08T00:00:00Z', 'updated_at': '2026-02-08T00:00:00Z',
                    'timeout_seconds': 3600, 'max_retries': 0, 'attempt': 0, 'cancel_requested': False}
            elif path == BACKTEST + '/' + IDENT:
                data = report
            elif path == BACKTEST + '/' + IDENT + '/rows':
                kind = query.get('kind', [''])[0]
                if kind == 'equity':
                    rows = equity
                elif kind == 'trades':
                    rows = [{'date': '2026-01-06', 'code': '000001', 'side': 'buy', 'shares': 100,
                             'price': 10, 'amount': 1000, 'fee': .3, 'reason': f'成交{index}'} for index in range(120)]
                elif kind == 'positions':
                    rows = [{'date': '2026-01-06', 'code': '000001', 'shares': 100, 'weight': .1, 'stale': False}]
                else:
                    rows = []
                offset, limit = int(query.get('offset', ['0'])[0]), int(query.get('limit', ['50'])[0])
                data = {'records': rows[offset:offset + limit], 'total': len(rows), 'offset': offset, 'limit': limit}
            elif path == BACKTEST + '/' + IDENT + '/export':
                route.fulfill(content_type='application/json', body=json.dumps({**report, 'trades': [dict(i=i) for i in range(120)]}))
                return
            elif path == '/api/v1/tasks':
                data = [state['task']] if state['task'] and query.get('task_type') == ['backtest.run'] else []
            elif path.endswith('/events'):
                route.fulfill(content_type='text/event-stream', body=': test stream\n\n')
                return
            elif path == '/api/v1/tasks/task-workspace-fixture':
                data = state['task']
            elif path == '/api/v1/web/paper-trading/summary':
                empty = {'records': [], 'columns': [], 'total': 0}
                data = {'user_id': OWNER, 'is_available': True,
                    'account': {'cash': 125000, 'total_assets': 125000},
                    **{key: deepcopy(empty) for key in ['positions', 'orders', 'nav_history', 'decisions', 'cash_flows']},
                    **{key: {} for key in ['risk_report', 'execution_diagnostics', 'trading_settings', 'profile',
                                           'profile_options', 'backfill_status', 'ai_reliability', 'scheduler', 'model_context']},
                    'profile_complete': False, 'order_snapshot_dates': [], 'position_snapshot_dates': []}
            elif path == '/api/v1/web/paper-trading/proposals':
                data = {'records': [], 'total': 0}
            elif path == '/api/v1/web/agent/sessions':
                data = {'records': [{'conversation_id': 'strategy-browser-session', 'user_id': OWNER,
                    'title': '研究对话', 'status': 'active', 'language': 'zh',
                    'created_at': '2026-02-08', 'updated_at': '2026-02-08', 'last_message_at': '', 'last_run_id': ''}], 'total': 1, 'limit': 30, 'offset': 0}
            elif path.endswith('/messages') or path == '/api/v1/web/agent/pending-actions':
                data = {'records': [], 'total': 0, 'offset': 0, 'limit': 30}
            elif path.endswith('/strategy-proposal'):
                data = {'available': False, 'proposal': None, 'versions': []}
            else:
                status, data = 404, {'detail': 'unexpected fixture endpoint ' + path}
            route.fulfill(status=status, json={'success': status == 200, 'data': data, 'request_id': 'fixture'})

        page.route('**/api/v1/**', reply)
        page.goto(os.environ['BACKTEST_BROWSER_URL'] + '/paper-trading')
        page.get_by_role('heading', name='我的策略', exact=True).wait_for()
        try:
            yield page, calls, state
        finally:
            out = Path('browser-results'); out.mkdir(exist_ok=True)
            page.screenshot(path=str(out / f'{request.node.name}.png'), full_page=True)
            (out / f'{request.node.name}.json').write_text(json.dumps({'errors': errors, 'calls': calls}, ensure_ascii=False, indent=2))
            context.close(); browser.close()
        assert not errors, errors


def test_workspace_real_report_paging_versions_rerun_recovery_and_delete(workspace_page, tmp_path):
    from playwright.sync_api import expect
    page, calls, state = workspace_page
    assert urlparse(page.url).path == '/strategies'
    expect(page.locator('.strategy-stats')).to_contain_text('42')
    expect(page.locator('.strategy-stats')).to_contain_text('15.00%')
    card = page.locator('.strategy-summary-card')
    expect(card).to_contain_text('20.00%')
    expect(card).to_contain_text('动态权重')
    expect(card).to_contain_text('当前配置待回测')
    Path('browser-results').mkdir(exist_ok=True)
    page.screenshot(path='browser-results/strategy-workspace-cards.png', full_page=True)
    card.get_by_role('button', name='详情', exact=True).click()
    drawer = page.get_by_role('dialog')
    expect(drawer).to_contain_text('下面展示的是旧版本报告')
    drawer.get_by_role('tab', name='全部成交', exact=True).click()
    drawer.locator('.ant-pagination-item-2').click()
    page.wait_for_function("document.body.innerText.includes('成交50')")
    assert any(path.endswith('/rows') and query.get('offset') == ['50'] for _, path, _, query in calls)
    drawer.get_by_role('tab', name='逐日持仓', exact=True).click()
    expect(drawer).to_contain_text('000001')
    page.screenshot(path='browser-results/strategy-workspace-report.png', full_page=True)
    with page.expect_download() as download:
        drawer.get_by_role('button', name='导出完整JSON报告').click()
    destination = tmp_path / 'report.json'; download.value.save_as(destination)
    assert len(json.loads(destination.read_text())['trades']) == 120
    drawer.get_by_role('button', name='重新回测', exact=True).click()
    page.get_by_role('button', name='确认运行', exact=True).click()
    expect(page.locator('.strategy-task-panel')).to_contain_text('正在计算真实数据范围')
    page.reload()
    expect(page.locator('.strategy-task-panel')).to_contain_text('task-workspace-fixture')
    assert len([call for call in calls if call[0] == 'POST' and call[1].endswith('/rerun')]) == 1
    state['task'].update(status='failed', message='测试：行情缺口', progress=1, finished_at='2026-02-08T00:01:00Z')
    expect(page.locator('.strategy-task-panel')).to_contain_text('测试：行情缺口', timeout=12000)
    drawer = page.get_by_role('dialog')
    expect(drawer).to_contain_text('旧报告保留', timeout=12000)
    expect(drawer.get_by_role('button', name='重新回测', exact=True)).to_be_enabled()
    drawer.get_by_role('button', name='删除策略', exact=True).click()
    page.get_by_role('button', name='确认删除', exact=True).click()
    expect(page.get_by_text('还没有保存的策略', exact=True)).to_be_visible()
    assert any(method == 'DELETE' and body['expected_version'] == 2 for method, _, body, _ in calls)
    assert not any(method == 'DELETE' and path.startswith(BACKTEST) for method, path, _, _ in calls)


def test_new_strategy_opens_unsent_agent_draft_and_source_report_save_retries(workspace_page):
    from playwright.sync_api import expect
    page, calls, state = workspace_page
    page.get_by_role('button', name='新建策略', exact=True).click()
    modal = page.get_by_role('dialog')
    modal.get_by_label('策略名称', exact=True).fill('测试低波动')
    modal.get_by_label('因子', exact=True).click()
    page.locator('.ant-select-item-option').filter(has_text='MA5').click()
    modal.get_by_role('button', name='去 AI Agent 确认').click()
    composer = page.get_by_test_id('agent-composer-input')
    expect(composer).to_have_value(__import__('re').compile('测试低波动'))
    expect(composer).to_have_value(__import__('re').compile('2026-01-05 至 2026-02-05'))
    assert not any(method == 'POST' and ('/messages' in path or path == '/api/v1/tasks') for method, path, _, _ in calls)
    page.goto(os.environ['BACKTEST_BROWSER_URL'] + '/strategies?report=' + IDENT)
    page.get_by_role('button', name='保存到我的策略', exact=True).click()
    modal = page.get_by_role('dialog').last
    modal.get_by_label('策略名称', exact=True).fill('由报告保存的策略')
    state['fail_next_save'] = True
    modal.get_by_role('button', name='确认保存', exact=True).click()
    expect(modal).to_contain_text('保存暂时不可用')
    modal.get_by_role('button', name='确认保存', exact=True).click()
    expect(page.get_by_text('已保存到我的策略，原回测报告保持不变')).to_be_visible()
    assert len(state['save_requests']) == 2
    assert state['save_requests'][0]['request_id'] == state['save_requests'][1]['request_id']
    assert not any(method == 'POST' and path == BACKTEST for method, path, _, _ in calls)


def test_strategy_workspace_mobile_keeps_cards_and_report_inside_viewport(workspace_page):
    from playwright.sync_api import expect
    page, _, _ = workspace_page
    page.set_viewport_size({'width': 390, 'height': 844})
    page.reload()
    expect(page.get_by_role('heading', name='我的策略', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='打开页面导航', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    card = page.locator('.strategy-summary-card')
    expect(card).to_contain_text('20.00%')
    box = card.bounding_box()
    assert box and box['x'] >= 0 and box['x'] + box['width'] <= 390
    page.screenshot(path='browser-results/strategy-workspace-mobile.png', full_page=True)
    card.get_by_role('button', name='详情', exact=True).click()
    drawer = page.get_by_role('dialog')
    expect(drawer).to_contain_text('下面展示的是旧版本报告')
    # Content can appear before the slide-in transition finishes. Wait for the
    # drawer to enter the viewport and allow subpixel layout rounding.
    page.wait_for_function('''el => {
        const box = el.getBoundingClientRect();
        return box.x >= -1 && box.right <= window.innerWidth + 1;
    }''', arg=drawer.element_handle())
    box = drawer.bounding_box()
    assert box and box['width'] <= 391 and box['x'] >= -1 and box['x'] + box['width'] <= 391
    drawer.get_by_role('tab', name='逐日持仓', exact=True).click()
    expect(drawer).to_contain_text('000001')
    page.screenshot(path='browser-results/strategy-workspace-mobile-report.png', full_page=True)


def test_previous_account_remains_available_as_secondary_entry_without_mutation(workspace_page):
    from playwright.sync_api import expect
    page, calls, _ = workspace_page
    assert urlparse(page.url).path == '/strategies'
    page.get_by_role('button', name='历史模拟账户', exact=True).click()
    expect(page.get_by_role('heading', name='AI 模拟盘', exact=True)).to_be_visible()
    assert urlparse(page.url).path == '/paper-account'
    expect(page.locator('body')).to_contain_text('125,000.00')
    assert any(method == 'GET' and path == '/api/v1/web/paper-trading/summary' and query.get('user_id') == [OWNER]
               for method, path, _, query in calls)
    assert not any(method in {'POST', 'PUT', 'DELETE'} for method, _, _, _ in calls)


def test_failed_workspace_confirmations_can_retry_without_unhandled_rejection(workspace_page):
    from playwright.sync_api import expect
    page, calls, state = workspace_page
    card = page.locator('.strategy-summary-card')
    state['fail_next_write'] = {'method': 'POST', 'prefix': ROOT + '/' + STRATEGY_ID + '/rerun'}
    card.get_by_role('button', name='重新回测', exact=True).click()
    page.get_by_role('button', name='确认运行', exact=True).click()
    expect(page.get_by_text('测试：操作暂时不可用，请重试', exact=True)).to_be_visible()
    expect(page.get_by_role('dialog')).to_have_count(0)
    expect(card.get_by_role('button', name='重新回测', exact=True)).to_be_enabled()
    card.get_by_role('button', name='重新回测', exact=True).click()
    page.get_by_role('button', name='确认运行', exact=True).click()
    expect(page.locator('.strategy-task-panel')).to_contain_text('正在计算真实数据范围')
    requests = [body for method, path, body, _ in calls if method == 'POST' and path.endswith('/rerun')]
    assert len(requests) == 2 and requests[0]['request_id'] == requests[1]['request_id']
    state['task'].update(status='failed', message='测试：行情缺口', progress=1)
    expect(card.get_by_role('button', name='删除', exact=True)).to_be_enabled(timeout=12000)
    state['fail_next_write'] = {'method': 'DELETE', 'prefix': ROOT + '/' + STRATEGY_ID}
    card.get_by_role('button', name='删除', exact=True).click()
    page.get_by_role('button', name='确认删除', exact=True).click()
    expect(page.get_by_role('dialog')).to_have_count(0)
    expect(card.get_by_role('button', name='删除', exact=True)).to_be_enabled()
    card.get_by_role('button', name='删除', exact=True).click()
    page.get_by_role('button', name='确认删除', exact=True).click()
    expect(page.get_by_text('还没有保存的策略', exact=True)).to_be_visible()
    requests = [body for method, path, body, _ in calls if method == 'DELETE' and path == ROOT + '/' + STRATEGY_ID]
    assert len(requests) == 2 and requests[0]['request_id'] == requests[1]['request_id']


def test_failed_advanced_save_and_run_keep_the_same_request_and_draft(workspace_page):
    from playwright.sync_api import expect
    page, calls, state = workspace_page
    page.goto(os.environ['BACKTEST_BROWSER_URL'] + '/backtests?strategy=' + STRATEGY_ID)
    expect(page.get_by_label('名称', exact=True)).to_have_value(SPEC['name'])
    state['fail_next_write'] = {'method': 'PUT', 'prefix': ROOT + '/'}
    page.get_by_role('button', name='另存为新策略', exact=True).click()
    page.get_by_role('dialog').get_by_role('button', name=re.compile('确.?定')).click()
    expect(page.get_by_role('dialog')).to_have_count(0)
    expect(page.get_by_label('名称', exact=True)).to_have_value(SPEC['name'])
    page.get_by_role('button', name='另存为新策略', exact=True).click()
    page.get_by_role('dialog').get_by_role('button', name=re.compile('确.?定')).click()
    expect(page.get_by_text('已保存个人策略', exact=True)).to_be_visible()
    requests = [(path, body) for method, path, body, _ in calls if method == 'PUT' and path.startswith(ROOT + '/')]
    assert len(requests) == 2 and requests[0] == requests[1]
    state['fail_next_write'] = {'method': 'POST', 'prefix': BACKTEST}
    page.get_by_role('button', name='运行策略回测', exact=True).click()
    page.get_by_role('dialog').get_by_role('button', name=re.compile('确.?定')).click()
    expect(page.get_by_role('dialog')).to_have_count(0)
    expect(page.get_by_role('button', name='运行策略回测', exact=True)).to_be_enabled()
    page.get_by_role('button', name='运行策略回测', exact=True).click()
    page.get_by_role('dialog').get_by_role('button', name=re.compile('确.?定')).click()
    expect(page.get_by_text('已提交本人研究任务', exact=True)).to_be_visible()
    requests = [body for method, path, body, _ in calls if method == 'POST' and path == BACKTEST]
    assert len(requests) == 2 and requests[0]['request_id'] == requests[1]['request_id']
