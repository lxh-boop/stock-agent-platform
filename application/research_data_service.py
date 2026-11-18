"""Data discovery and stock pools backed by the current account's real database."""
from __future__ import annotations

from datetime import date, datetime
import math
import re
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.alpha158_contract import FEATURE_COLUMNS
from core.factor_expressions import alpha158_definitions, evaluate_expression, expression_templates, validate_expression
from database.repositories.research_data_repository import PRICE_FIELDS, STOCK_FIELDS, ResearchDataRepository

TABLE_NAMES = ('alpha158_daily', 'market_data_daily', 'stock_basic', 'custom_factors')
UNAVAILABLE_SOURCES = [
    {'source': 'financial_factors', 'reason': '源 Strategy 财务因子表尚未导入，不能把财务字段映射成 Alpha158'},
    {'source': 'industry_membership', 'reason': '未接入带生效日期的行业成分数据；证券主表当前行业不能用于历史时点股票池'},
    {'source': 'index_constituents', 'reason': '未接入历史指数成分数据；不能把当前个人股票覆盖范围称为沪深300等历史成分'},
]


class PoolFilter(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    table: Literal['alpha158_daily'] = 'alpha158_daily'
    field: str = Field(max_length=30)
    op: Literal['gt', 'ge', 'lt', 'le', 'eq', 'between']
    value: float
    upper: float | None = None

    @model_validator(mode='after')
    def check(self):
        if self.field not in FEATURE_COLUMNS:
            raise ValueError('股票池仅支持目录中的已存 Alpha158 因子过滤')
        if self.op == 'between' and (self.upper is None or self.upper < self.value):
            raise ValueError('between 必须提供不小于 value 的 upper')
        if self.op != 'between' and self.upper is not None:
            raise ValueError('只有 between 可以提供 upper')
        return self


class PoolGroup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['list', 'industry', 'index']
    ids: list[str] = Field(default_factory=list, max_length=200)
    names: list[str] = Field(default_factory=list, max_length=40)
    codes: list[str] = Field(default_factory=list, max_length=50)


def parse_day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r'\d{8}|\d{4}-\d{2}-\d{2}', value, flags=re.ASCII):
        raise ValueError('交易日必须是 YYYYMMDD 或 YYYY-MM-DD')
    try:
        return datetime.strptime(value, '%Y%m%d' if '-' not in value else '%Y-%m-%d').date()
    except ValueError:
        raise ValueError('交易日不是有效日期') from None


def normalize_security(value: str) -> str:
    text = str(value or '').strip().upper()
    match = re.fullmatch(r'([0-9]{6})(?:\.(SH|SZ|BJ|XSHG|XSHE|XBSE))?', text)
    if not match:
        raise ValueError('证券代码需为六位股票代码，可带 .SH/.SZ/.BJ 或 .XSHG/.XSHE 后缀')
    code, suffix = match.groups()
    market = 'BJ' if code.startswith(('4', '8', '92')) else 'SH' if code.startswith(('6', '9')) else 'SZ' if code.startswith(('0', '3')) else None
    actual = {'XSHG': 'SH', 'XSHE': 'SZ', 'XBSE': 'BJ'}.get(suffix, suffix)
    if suffix and actual != market:
        raise ValueError('代码后缀与股票市场不匹配；当前工具不把指数代码当作股票代码读取')
    return code


def _number(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


class ResearchDataService:
    def __init__(self, repository: ResearchDataRepository | None = None):
        self.repository = repository or ResearchDataRepository()

    def list_factor_tables(self):
        raw = self.repository.catalog()['tables']
        descriptions = {
            'alpha158_daily': ('Alpha158 已存因子', '本人日更保存的当前公式版本因子值，不含未来标签', ['date', 'code']),
            'market_data_daily': ('行情日表', '本人数据库已保存的日行情；不补造缺失交易日', ['date', 'code']),
            'stock_basic': ('证券主数据', '本人证券主表的当前信息；行业等字段不是历史时点快照', ['code']),
            'custom_factors': ('自定义因子', '本人已保存表达式；按指定日及以前行情即时计算，不声称已有历史因子值表', ['date', 'code']),
        }
        tables = []
        for table, (title, description, keys) in descriptions.items():
            coverage = raw.get(table)
            populated = bool(coverage and (coverage.get('row_count') or
                             (coverage.get('definition_count') and coverage.get('start_date'))))
            tables.append({'table': table, 'title': title, 'description': description,
                           'unique_key': keys, 'coverage': coverage, 'status': 'ready' if populated else 'empty',
                           'research_supported': table == 'alpha158_daily',
                           'read_mode': 'computed' if table == 'custom_factors' else 'stored'})
        return {'tables': tables, 'scope': 'current_user', 'unavailable_sources': UNAVAILABLE_SOURCES,
                'message': '只列出当前平台可读取的数据类型；empty 表示本人尚无数据，不代表全市场数据。'}

    def list_factor_fields(self, tables: list[str], *, limit: int = 40, offset: int = 0):
        if not 1 <= len(tables) <= 4 or not 1 <= limit <= 80 or not 0 <= offset <= 400:
            raise ValueError('一次查询 1～4 张表，字段 limit 为 1～80，offset 为 0～400')
        if any(not re.fullmatch('[a-z_][a-z0-9_]{0,63}', name) for name in tables):
            raise ValueError('表名必须来自 list_factor_tables 返回的目录')
        definitions = self.repository.definitions()
        custom = [item for item in definitions if item['source'] == 'custom']
        result = []
        for table in dict.fromkeys(tables):
            if table not in TABLE_NAMES:
                continue
            if table == 'alpha158_daily':
                fields = [{'name': item['name'], 'description': item['description'], 'type': 'number',
                           'nullable': True, 'research_supported': True} for item in alpha158_definitions()]
            elif table == 'custom_factors':
                fields = [{'name': item['factor_id'], 'display_name': item['name'], 'description': item['description'],
                           'type': 'number', 'nullable': True, 'research_supported': False,
                           'lookback_rows': validate_expression(item['expression'])['lookback_rows']} for item in custom]
            else:
                names = PRICE_FIELDS if table == 'market_data_daily' else STOCK_FIELDS
                fields = [{'name': name, 'type': 'number' if table == 'market_data_daily' or name in ('is_st', 'market_cap') else 'string',
                           'nullable': True, 'research_supported': False} for name in names]
            page = fields[offset:offset + limit]
            result.append({'table': table, 'fields': page, 'total': len(fields), 'offset': offset,
                           'returned': len(page), 'truncated': offset + len(page) < len(fields),
                           'next_offset': offset + len(page) if offset + len(page) < len(fields) else None})
        return {'tables': result, 'not_found': [name for name in dict.fromkeys(tables) if name not in TABLE_NAMES],
                'templates': expression_templates() if 'custom_factors' in tables else [],
                'message': '自定义字段用 factor_id 查询；research_supported=false 的字段暂不能用于现有回测规格。'}

    def read_factor_data(self, table: str, fields: list[str], date: str, security: str):
        if table not in TABLE_NAMES:
            raise ValueError('该数据表尚未接入当前平台，请先查看 list_factor_tables；财务/行业/指数成分表不会被替代为其他数据')
        if not 1 <= len(fields) <= 12 or any(not isinstance(field, str) or len(field) > 128 for field in fields):
            raise ValueError('每次只能读取 1～12 个目录中的字段')
        fields = list(dict.fromkeys(fields))
        day, code = parse_day(date), normalize_security(security)
        if table == 'custom_factors':
            return self._read_custom(fields, day=day, code=code)
        rows = self.repository.read_row(table, fields, day=day, code=code)
        output = []
        for row in rows:
            record = {'code': code, **{field: _number(row.get(field)) for field in fields}}
            if table != 'stock_basic':
                record['date'] = day.isoformat()
            output.append(record)
        return {'table': table, 'fields': fields, 'requested_date': day.isoformat(), 'security': code,
                'data': output, 'returned': len(output), 'status': 'ready' if output else 'empty',
                'source': 'stored', 'research_supported': table == 'alpha158_daily',
                'message': ('证券主表是当前信息，输入日期不用于历史快照查询。' if table == 'stock_basic'
                            else '仅返回指定交易日已保存的本人数据，不向前或向后填补缺失记录。')}

    def _read_custom(self, fields, *, day, code):
        custom = {item['factor_id']: item for item in self.repository.definitions() if item['source'] == 'custom'}
        if any(field not in custom for field in fields):
            raise ValueError('自定义因子不存在或不属于当前账号，请用 list_factor_fields 返回的 factor_id')
        prepared = {field: validate_expression(custom[field]['expression']) for field in fields}
        lookback = max(item['lookback_rows'] for item in prepared.values())
        required = sorted({field for item in prepared.values() for field in item['fields']})
        rows = self.repository.custom_history(day=day, code=code, fields=required, lookback=lookback)
        output = []
        if rows and str(rows[0]['date'])[:10] == day.isoformat():
            frame = pd.DataFrame.from_records(rows).sort_values('date').reset_index(drop=True)
            record = {'code': code, 'date': day.isoformat()}
            for field in fields:
                value = evaluate_expression(prepared[field]['expression'], frame).iloc[-1]
                record[field] = float(value) if math.isfinite(value) else None
            output.append(record)
        return {'table': 'custom_factors', 'fields': fields, 'requested_date': day.isoformat(),
                'security': code, 'data': output, 'returned': len(output), 'status': 'ready' if output else 'empty',
                'source': 'computed', 'research_supported': False,
                'message': '仅用该日及以前的本人已存行情计算；空值表示历史不足。自定义因子尚未接入回测引擎。'}

    def screen_stock_pool(self, *, as_of: str | None = None, codes: list[str] | None = None,
                          filters: list[dict] | None = None, groups: list[dict] | None = None, limit: int = 100):
        if not 1 <= limit <= 200 or (codes is not None and (not codes or len(codes) > 1000)):
            raise ValueError('股票池 limit 为 1～200，指定代码集合需为 1～1000 项')
        groups = groups or []
        filters = filters or []
        if len(groups) > 5 or len(filters) > 8:
            raise ValueError('最多使用 5 个集合组和 8 个因子过滤条件')
        selected = set(map(normalize_security, codes)) if codes is not None else None
        for raw in groups:
            group = PoolGroup.model_validate(raw)
            if group.kind == 'industry':
                raise ValueError('行业股票池不可用：尚未导入带生效日期的行业成分，不能用当前主数据替代历史归属')
            if group.kind == 'index':
                raise ValueError('指数成分股票池不可用：尚未导入历史指数成分，不能推测沪深300等成员')
            if not group.ids or group.codes or group.names:
                raise ValueError('list 组必须只提供非空 ids 证券清单')
            members = set(map(normalize_security, group.ids))
            selected = members if selected is None else selected & members
        normalized = [PoolFilter.model_validate(item).model_dump() for item in filters]
        result = self.repository.screen(as_of=parse_day(as_of) if as_of else None,
                                        codes=sorted(selected) if selected is not None else None,
                                        filters=normalized, limit=limit)
        return {**result, 'requested_date': as_of, 'scope': 'current_user_alpha158_coverage',
                'filters': normalized, 'earliest_backtest_start': result['as_of'],
                'message': '这是本人指定时点因子覆盖范围的筛选结果，不代表全市场、行业或指数成分。不得把该池用于早于 as_of 的回测起点；truncated=true 时返回的 codes 仅是部分结果。'}
