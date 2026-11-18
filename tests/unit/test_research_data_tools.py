from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from agent_graph.tools.research_data_tools import build_research_data_tools
from application.research_data_service import ResearchDataService, normalize_security, parse_day
from core.user_scope import current_owner, user_scope
from database.repositories.research_data_repository import ResearchDataRepository


class FakeRepository:
    def definitions(self):
        return [{'factor_id': 'custom:mine', 'name': '自定义趋势', 'expression': 'shift(close, 1)',
                 'description': '本人表达式', 'source': 'custom'}]

    def catalog(self):
        return {'tables': {'alpha158_daily': {'row_count': 30, 'start_date': '2026-01-01', 'end_date': '2026-01-05'},
                           'market_data_daily': {'row_count': 0}, 'stock_basic': {'row_count': 3},
                           'custom_factors': {'definition_count': 1, 'start_date': '2026-01-01'}},
                'definitions': self.definitions()}

    def custom_history(self, **kwargs):
        assert kwargs == {'day': date(2026, 1, 5), 'code': '000001', 'fields': ['close'], 'lookback': 1}
        return [{'date': date(2026, 1, 5), 'code': '000001', 'close': 12},
                {'date': date(2026, 1, 2), 'code': '000001', 'close': 10}]

    def screen(self, **kwargs):
        self.screen_args = kwargs
        return {'as_of': '2026-01-05', 'codes': ['000002'], 'total': 1, 'returned': 1, 'truncated': False,
                'coverage': {'start_date': '2026-01-01', 'end_date': '2026-01-05'}, 'universe_count': 3}


def test_discovery_has_real_status_bounded_fields_and_unsupported_custom_research():
    service = ResearchDataService(FakeRepository())
    catalog = service.list_factor_tables()
    by_name = {item['table']: item for item in catalog['tables']}
    assert by_name['market_data_daily']['status'] == 'empty'
    assert by_name['alpha158_daily']['research_supported'] is True
    assert by_name['custom_factors']['research_supported'] is False
    fields = service.list_factor_fields(['alpha158_daily', 'custom_factors', 'financial_income'], limit=2)
    assert fields['tables'][0]['total'] == 158 and fields['tables'][0]['next_offset'] == 2
    custom = fields['tables'][1]['fields'][0]
    assert custom['name'] == 'custom:mine' and custom['research_supported'] is False
    assert fields['templates'] and fields['not_found'] == ['financial_income']
    assert len(service.list_factor_fields(['alpha158_daily'], offset=156)['tables'][0]['fields']) == 2


def test_custom_read_uses_past_history_and_does_not_claim_stored_values():
    service = ResearchDataService(FakeRepository())
    result = service.read_factor_data('custom_factors', ['custom:mine'], '20260105', '000001.XSHE')
    assert result['data'] == [{'code': '000001', 'date': '2026-01-05', 'custom:mine': 10.}]
    assert result['source'] == 'computed' and result['research_supported'] is False
    with pytest.raises(ValueError, match='不存在或不属于'):
        service.read_factor_data('custom_factors', ['custom:other-account'], '20260105', '000001')


def test_pool_intersects_list_groups_and_discloses_point_in_time_boundary():
    repo = FakeRepository()
    service = ResearchDataService(repo)
    result = service.screen_stock_pool(as_of='20260106', codes=['000001', '000002'],
                                      groups=[{'kind': 'list', 'ids': ['000002.XSHE', '600000.XSHG']}],
                                      filters=[{'field': 'KMID', 'op': 'gt', 'value': 0}])
    assert repo.screen_args['codes'] == ['000002']
    assert repo.screen_args['as_of'] == date(2026, 1, 6)
    assert result['earliest_backtest_start'] == result['as_of'] == '2026-01-05'
    assert 'poolId' not in result and result['scope'] == 'current_user_alpha158_coverage'
    for kind in ('industry', 'index'):
        with pytest.raises(ValueError, match='不可用'):
            service.screen_stock_pool(groups=[{'kind': kind, 'names': ['银行']}])


@pytest.mark.parametrize('value,expected', [('000001.XSHE','000001'), ('600000.XSHG','600000'), ('920001.BJ','920001'), ('000001','000001')])
def test_source_style_stock_ids(value, expected):
    assert normalize_security(value) == expected


@pytest.mark.parametrize('value', ['000001.SH', '600000.SZ', "000001'; DROP TABLE stock_basic;--", '../secret', '１２３４５６'])
def test_security_suffix_must_not_silently_turn_index_into_another_stock(value):
    with pytest.raises(ValueError):
        normalize_security(value)


def test_read_rejects_sql_and_private_payload_fields_before_connecting():
    with user_scope('alice'):
        repo = ResearchDataRepository()
        with pytest.raises(ValueError, match='白名单'):
            repo.read_row('alpha158_daily', ['source_payload'], day=date(2026, 1, 1), code='000001')
        with pytest.raises(ValueError, match='白名单'):
            repo.read_row('user_account', ['password_hash'], day=date(2026, 1, 1), code='000001')
    for value in ('20260230', '2026-1-1', '2026-01-01;select'):
        with pytest.raises(ValueError):
            parse_day(value)


def test_tools_bind_owner_reject_other_context_and_never_leak_errors(monkeypatch):
    seen = []
    def catalog(self):
        seen.append(current_owner())
        return {'tables': []}
    monkeypatch.setattr(ResearchDataService, 'list_factor_tables', catalog)
    tools = {tool.name: tool for tool in build_research_data_tools({'user_id': 'alice'})}
    assert json.loads(tools['list_factor_tables'].invoke({}))['success'] is True
    assert seen == ['alice'] and not current_owner()
    with user_scope('bob'):
        assert not json.loads(tools['list_factor_tables'].invoke({}))['success']
    assert seen == ['alice']
    def failure(self):
        raise RuntimeError('postgresql://username:private-test-secret@database')
    monkeypatch.setattr(ResearchDataService, 'list_factor_tables', failure)
    text = tools['list_factor_tables'].invoke({})
    assert not json.loads(text)['success'] and 'private-test-secret' not in text
    with pytest.raises(ValidationError):
        tools['screen_stock_pool'].invoke({'user_id': 'bob'})
    with pytest.raises(ValidationError):
        tools['screen_stock_pool'].invoke({'limit': 999999})


def test_tools_return_parseable_bounded_json_when_large(monkeypatch):
    monkeypatch.setattr(ResearchDataService, 'list_factor_tables', lambda self: {'text': '中' * 65000})
    tool = next(tool for tool in build_research_data_tools({'user_id': 'alice'}) if tool.name == 'list_factor_tables')
    result = json.loads(tool.invoke({}))
    assert result['success'] is False and '过大' in result['message']
