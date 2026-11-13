"""Temporary, reviewed final corrections; removed after migration acceptance."""
import ast
import re
from pathlib import Path


def replace_one(path, old, new):
    target = Path(path)
    source = target.read_text(encoding='utf-8')
    assert source.count(old) == 1, (path, old)
    target.write_text(source.replace(old, new), encoding='utf-8')


path = Path('agent_graph/services/strategy_implementation_service.py')
source = path.read_text(encoding='utf-8')
lines = source.splitlines(keepends=True)
funcs = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == '_build_backtest_bundle']
for node in funcs:
    body = ''.join(lines[node.lineno-1:node.end_lineno])
    if '"status": "blocked"' not in body:
        raise RuntimeError('Strategy preview must not claim an unexecuted backtest passed')
    fixed, count = re.subn(r'("baseline_config"\s*:\s*)baseline_config\b', r'\1raw_config', body)
    if count:
        assert 'raw_config =' in body
        lines[node.lineno-1:node.end_lineno] = [fixed]
path.write_text(''.join(lines), encoding='utf-8')
fixture = '''"""Database integration tests share one initialization in a local temporary database only."""
import os
from pathlib import Path
import pytest

@pytest.fixture(scope="session")
def isolated_postgres_schema():
    if os.environ.get("STOCK_TEST_DATABASE") != "1":
        pytest.skip("requires isolated test database")
    psycopg = pytest.importorskip("psycopg")
    from database.postgres_config import PostgresSettings
    settings = PostgresSettings.from_env()
    if settings.database != "stock_test_isolation" or settings.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Refuse non-local or non-test database")
    path = Path(__file__).resolve().parents[2] / "database/postgres_migrations/001_baseline.sql"
    script = "\\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("\\\\"))
    with psycopg.connect(**settings.connection_kwargs()) as conn:
        conn.execute(script)
    yield
'''
path = Path('tests/integration/conftest.py')
assert not path.exists(), 'Do not overwrite an existing test fixture'
path.write_text(fixture, encoding='utf-8')
ast.parse(fixture)
for filename, name, auth in [('test_strategy_backtest_postgres.py','schema',False),('test_alpha158_postgres.py','schema',False),('test_account_datasets_postgres.py','initialize_schema',True)]:
    path = Path('tests/integration') / filename
    source = path.read_text(encoding='utf-8')
    lines = source.splitlines(keepends=True)
    nodes = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(nodes) == 1
    node = nodes[0]
    replacement = f'def {name}(isolated_postgres_schema):\n'
    if auth:
        replacement += '    from database.auth_schema import ensure_auth_schema\n    ensure_auth_schema()\n'
    replacement += '    yield\n'
    lines[node.lineno-1:node.end_lineno] = [replacement]
    path.write_text(''.join(lines), encoding='utf-8')
    ast.parse(path.read_text(encoding='utf-8'))
path = Path('.github/workflows/strategy-backtest-tests.yml')
source = path.read_text(encoding='utf-8')
source = source.replace('runs-on: ubuntu-latest', 'runs-on: ubuntu-24.04')
source = source.replace('tests/integration/test_strategy_backtest_postgres.py\n', 'tests/integration/test_strategy_backtest_postgres.py tests/integration/test_alpha158_postgres.py tests/integration/test_account_datasets_postgres.py\n')
path.write_text(source, encoding='utf-8')

page = 'frontend/src/pages/dashboard/BacktestPage.tsx'
replace_one(page,
    'const values=await form.validateFields(), spec=toStudySpec(values,kind)',
    'await form.validateFields()\n    // 折叠面板尚未挂载的参数也必须保留，校验结果只含已注册字段。\n    const spec=toStudySpec(form.getFieldsValue(true),kind)')
replace_one(page,
    'const values=await form.validateFields(),spec=toBacktestSpec(values)',
    'await form.validateFields()\n    const spec=toBacktestSpec(form.getFieldsValue(true))')
utils = 'frontend/src/utils/backtest.ts'
replace_one(utils, "bt.sizing?.proportions.join(', ')", "bt.sizing?.proportions?.join(', ')")
replace_one(utils, 'if (pool_schedule_text?.trim()) {\n    try { pool = JSON.parse(pool_schedule_text) }',
    "if (pool_schedule_text !== undefined) {\n    try { pool = pool_schedule_text.trim() ? JSON.parse(pool_schedule_text) : [] }")
watch = 'frontend/src/components/dashboard/ResearchForm.tsx'
for expression in ("['weighting', 'method']", "['sizing', 'method']", "'timing_enabled'"):
    replace_one(watch, f'Form.useWatch({expression}, form)', f'Form.useWatch({expression}, {{ form, preserve: true }})')
tests = Path('frontend/src/tests/backtest.test.ts')
tests.write_text(tests.read_text(encoding='utf-8') + '''

it('imports omitted optional sizing proportions without crashing', () => {
  const partial = { ...form, sizing: { method: 'factor', field: 'MA5' } }
  expect(fromStudySpec(partial as Parameters<typeof fromStudySpec>[0]).proportions_text).toBe('')
})

it('clears inherited historical membership snapshots when the editor is emptied', () => {
  const value = { ...form, pool_schedule: [{ as_of: '2026-01-01', stock_codes: ['000001'] }] }
  expect(toBacktestSpec({ ...fromStudySpec(value), ...form, pool_schedule: value.pool_schedule, pool_schedule_text: '' }).pool_schedule).toEqual([])
})
''', encoding='utf-8')
browser = 'tests/browser/test_strategy_research_browser.py'
replace_one(browser, "    page,calls,strategies=browser_page\n    page.get_by_role('button',name='另存为新策略')", """    page,calls,strategies=browser_page
    # 高级折叠面板始终保持关闭，验证保存和再次编辑不能丢失导入参数。
    page.get_by_role('button',name='导入 / 查看 JSON 参数').click()
    dialog=page.get_by_role('dialog')
    dialog.locator('textarea').fill(json.dumps(SPEC,ensure_ascii=False))
    dialog.get_by_role('button',name='应用到表单').click()
    page.get_by_role('button',name='另存为新策略')""")
replace_one(browser, '    assert len(strategies)==1\n', """    assert len(strategies)==1
    assert strategies[0]['spec']['weighting']==SPEC['weighting']
    assert strategies[0]['spec']['sizing']==SPEC['sizing']
    assert strategies[0]['spec']['timing']==SPEC['timing']
""")
replace_one(browser, "    assert strategies[0]['spec']['name']=='编辑后的个人策略'\n", """    assert strategies[0]['spec']['name']=='编辑后的个人策略'
    assert strategies[0]['spec']['weighting']==SPEC['weighting']
    assert strategies[0]['spec']['sizing']==SPEC['sizing']
    assert strategies[0]['spec']['timing']==SPEC['timing']
""")
replace_one(browser, "    assert request['spec']['start_date']==request['spec']['end_date']\n", """    assert request['spec']['start_date']==request['spec']['end_date']
    assert request['spec']['weighting']==SPEC['weighting']
    assert request['spec']['sizing']==SPEC['sizing']
    assert request['spec']['timing']==SPEC['timing']
""")
replace_one(page, '<Button type="primary" disabled={!catalog.data?.coverage||Boolean(active)}',
    '<Button type="primary" aria-label={`运行${studyLabels[kind]}`} data-testid={`run-research-${kind}`} disabled={!catalog.data?.coverage||Boolean(active)}')
replace_one(page, '>运行{studyLabels[kind]}</Button>', '>{`运行${studyLabels[kind]}`}</Button>')
replace_one(browser,'def browser_page():','def browser_page(request):')
replace_one(browser, '        yield page,calls,strategies\n        assert not errors,errors\n        context.close();browser.close()', '''        try:
            yield page,calls,strategies
        finally:
            result=Path('browser-results')
            result.mkdir(exist_ok=True)
            prefix=result / request.node.name
            page.screenshot(path=str(prefix)+'.png',full_page=True)
            Path(str(prefix)+'.html').write_text(page.content(),encoding='utf-8')
            Path(str(prefix)+'.json').write_text(json.dumps({'buttons':page.get_by_role('button').all_text_contents(),'body':page.locator('body').inner_text(),'errors':errors,'calls':calls},ensure_ascii=False,indent=2),encoding='utf-8')
            context.close();browser.close()
        assert not errors,errors''')
replace_one(browser, "    page.get_by_role('tab',name='因子分析',exact=True).click()\n", """    from playwright.sync_api import expect
    analysis_tab=page.get_by_role('tab',name='因子分析',exact=True)
    analysis_tab.click()
    expect(analysis_tab).to_have_attribute('aria-selected','true')
""")
replace_one(browser, "    page.get_by_role('tab',name='每日选股',exact=True).click()\n", """    screen_tab=page.get_by_role('tab',name='每日选股',exact=True)
    screen_tab.click()
    expect(screen_tab).to_have_attribute('aria-selected','true')
""")
ast.parse(Path(browser).read_text(encoding='utf-8'))
print('Final corrections applied; all business assertions retained and browser checks strengthened')
