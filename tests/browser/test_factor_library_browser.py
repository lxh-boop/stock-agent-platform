"""Exercise the built factor UI with deterministic API fixtures in real Chromium."""
from copy import deepcopy
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.skipif(not os.getenv('BACKTEST_BROWSER_URL'), reason='Browser preview not requested')
ROOT = '/api/v1/web/factors'


@pytest.fixture()
def factor_page(request):
    from core.factor_expressions import alpha158_definitions, expression_syntax, expression_templates
    from playwright.sync_api import sync_playwright

    factors = [{**item, 'factor_id': 'alpha158:' + item['name'], 'source': 'alpha158',
                'created_at': '2026-11-18T00:00:00Z', 'read_only': True}
               for item in alpha158_definitions()]
    calls, errors = [], []
    values = {'factor_id': 'alpha158:KMID', 'items': [{'code': '000001', 'date': '2026-11-17', 'value': 0.1234}],
              'total': 1, 'available_values': 1, 'as_of': '2026-11-17', 'source': 'stored',
              'status': 'ready', 'message': '测试数据库数值'}
    state = {'save_failure': False, 'values_failure': False}

    def reply(route):
        req = route.request
        path = urlparse(req.url).path
        params = parse_qs(urlparse(req.url).query)
        body = req.post_data_json if req.post_data else None
        calls.append((req.method, path, body, params))
        if path == '/api/v1/auth/me':
            route.fulfill(json={'user': {'user_id': 'factor-browser', 'display_name': '测试账号', 'role': 'user'}})
            return
        if path == '/api/v1/health':
            data = {'status': 'ok', 'version': 'test', 'deployment_mode': 'test'}
        elif path == '/api/v1/tasks':
            data = []
        elif path == ROOT and req.method == 'GET':
            data = {'items': factors, 'total': len(factors), 'system_count': 158,
                    'custom_count': len(factors) - 158, 'storage': 'postgresql',
                    'templates': expression_templates(), 'syntax': expression_syntax(),
                    'limits': {'preview_stocks': 3, 'preview_rows': 30}}
        elif path == ROOT + '/validate':
            if '__import__' in body['expression']:
                route.fulfill(json={'success': False, 'error': {'code': 'FACTOR_VALIDATION_FAILED', 'message': '表达式包含不允许的函数'}})
                return
            data = {'valid': True, **body, 'fields': ['close'], 'lookback_rows': 6, 'preview': {**values, 'source': 'computed'}}
        elif path == ROOT and req.method == 'POST':
            if state['save_failure']:
                state['save_failure'] = False
                route.fulfill(status=503, json={'detail': '保存暂时不可用，请重试'})
                return
            factor = {'factor_id': 'custom:browser-test', 'name': body['name'], 'expression': body['expression'],
                      'description': '用户自定义因子', 'category': '自定义', 'source': 'custom',
                      'created_at': '2026-11-18T00:00:00Z', 'read_only': False}
            factors.append(factor)
            data = {'factor': factor, 'replayed': False}
        elif path.startswith(ROOT + '/') and path.endswith('/values'):
            if state['values_failure']:
                route.fulfill(json={'success': False, 'error': {'code': 'FACTOR_CATALOG_UNAVAILABLE', 'message': '因子数据库暂时不可用'}})
                return
            data = deepcopy(values)
            data['items'][0]['code'] = params.get('code', ['000001'])[0]
        else:
            route.fulfill(status=404, json={'detail': 'Unexpected fixture endpoint'})
            return
        route.fulfill(json={'success': True, 'data': data, 'request_id': 'factor-browser'})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=os.getenv('BROWSER_EXECUTABLE') or None,
                                     chromium_sandbox=bool(os.getenv('BROWSER_EXECUTABLE')))
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, locale='zh-CN')
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/v1/**', reply)
        page.goto(os.environ['BACKTEST_BROWSER_URL'] + '/factors')
        page.get_by_role('heading', name='因子库', exact=True).wait_for()
        try:
            yield page, calls, state
        finally:
            dest = Path('browser-results')
            dest.mkdir(exist_ok=True)
            page.screenshot(path=str(dest / (request.node.name + '.png')), full_page=True)
            (dest / (request.node.name + '.json')).write_text(json.dumps({'calls': calls, 'errors': errors}, ensure_ascii=False, indent=2))
            browser.close()
        assert not errors, errors


def test_factor_catalog_values_filter_and_recovery(factor_page):
    from playwright.sync_api import expect
    page, calls, state = factor_page
    expect(page.get_by_text('共 158 个因子 · Alpha158 158 个 · 个人因子 0 个')).to_be_visible()
    page.get_by_role('searchbox', name='搜索因子').fill('KMID')
    page.get_by_role('button', name='KMID', exact=True).click()
    expect(page.get_by_text('0.1234', exact=True)).to_be_visible()
    code = page.get_by_role('searchbox', name='筛选股票代码')
    code.fill('600519')
    page.get_by_role('button', name='查询', exact=True).click()
    expect(page.get_by_role('cell', name='600519', exact=True)).to_be_visible()
    assert any(path.endswith('/values') and params.get('code') == ['600519'] for _, path, _, params in calls)
    state['values_failure'] = True
    page.get_by_role('button', name='刷新数值', exact=True).click()
    expect(page.get_by_text('因子数值读取失败', exact=True)).to_be_visible(timeout=15000)
    state['values_failure'] = False
    page.get_by_role('button', name='重试', exact=True).click()
    expect(page.get_by_text('0.1234', exact=True)).to_be_visible()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


def test_factor_template_validation_confirmation_and_save_retry(factor_page):
    from playwright.sync_api import expect
    page, calls, state = factor_page
    page.get_by_role('button', name='添加因子', exact=True).click()
    dialog = page.get_by_role('dialog')
    dialog.get_by_label('因子名称', exact=True).fill('五日动量')
    expression = dialog.get_by_label('Python 表达式', exact=True)
    expression.fill('__import__("os")')
    dialog.get_by_role('button', name='校验并继续', exact=True).click()
    expect(dialog.get_by_text('表达式包含不允许的函数', exact=True)).to_be_visible()
    assert not any(method == 'POST' and path == ROOT for method, path, _, _ in calls)
    dialog.get_by_role('button', name='使用模板', exact=True).first.click()
    expect(expression).not_to_have_value('__import__("os")')
    expression.fill('close / delay(close, 5) - 1')
    dialog.get_by_role('button', name='校验并继续', exact=True).click()
    expect(dialog.get_by_text('确认添加因子', exact=True)).to_be_visible()
    assert not any(method == 'POST' and path == ROOT for method, path, _, _ in calls)
    expect(dialog.get_by_text('0.1234', exact=True)).to_be_visible()
    state['save_failure'] = True
    dialog.get_by_role('button', name='确认添加', exact=True).click()
    expect(dialog.get_by_text('保存暂时不可用，请重试', exact=True)).to_be_visible()
    expect(dialog.get_by_role('button', name='确认添加', exact=True)).to_be_enabled()
    dialog.get_by_role('button', name='确认添加', exact=True).click()
    expect(dialog).to_have_count(0)
    expect(page.get_by_role('button', name='五日动量', exact=True)).to_be_visible()
    requests = [body for method, path, body, _ in calls if method == 'POST' and path == ROOT]
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0]['confirmed'] is True
    assert requests[0]['name'] == '五日动量'
    assert requests[0]['request_id'] and requests[0]['idempotency_key']
    assert 'owner_id' not in requests[0] and 'user_id' not in requests[0]
    page.reload()
    expect(page.get_by_text('共 159 个因子 · Alpha158 158 个 · 个人因子 1 个')).to_be_visible()
    page.get_by_role('searchbox', name='搜索因子').fill('五日动量')
    expect(page.get_by_role('button', name='五日动量', exact=True)).to_be_visible()
