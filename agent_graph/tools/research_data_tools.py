"""Source-compatible discovery tools backed by the platform's personal data service."""
from __future__ import annotations

import json

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from application.research_data_service import PoolFilter, PoolGroup, ResearchDataService
from core.user_scope import current_owner, user_scope, validate_owner


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class _FieldsArgs(_StrictArgs):
    tables: list[str] = Field(min_length=1, max_length=4, description='来自 list_factor_tables 的逻辑表名')
    limit: int = Field(default=40, ge=1, le=80, description='每张表字段分页大小；按 next_offset 继续读取')
    offset: int = Field(default=0, ge=0, le=400)


class _ReadArgs(_StrictArgs):
    table: str = Field(max_length=64, description='目录中的表名，不能传 SQL 或源仓库尚未导入的表')
    fields: list[str] = Field(min_length=1, max_length=12, description='list_factor_fields 返回的字段；custom_factors 使用 factor_id')
    date: str = Field(min_length=8, max_length=10, description='YYYYMMDD 或 YYYY-MM-DD；仅查这一天，证券主表当前资料除外')
    security: str = Field(max_length=16, description='六位股票代码或 000001.XSHE 等市场后缀形式；不支持指数')


class _PoolArgs(_StrictArgs):
    as_of: str | None = Field(default=None, max_length=10, description='查询时点；取不晚于该日的本人最新已存交易日，留空取本人最新日')
    codes: list[str] | None = Field(default=None, min_length=1, max_length=1000, description='可选点名股票清单，与本人覆盖范围及 groups 取交集')
    filters: list[PoolFilter] = Field(default_factory=list, max_length=8, description='已存 Alpha158 因子阈值；不支持未接入回测的自定义因子')
    groups: list[PoolGroup] = Field(default_factory=list, max_length=5, description='源集合规格：仅 kind=list 的 ids 可用，组内并集、组间交集；industry/index 会明确报缺数据')
    limit: int = Field(default=100, ge=1, le=200, description='最多返回股票数；必须查看 total/truncated，不把截断列表当完整股票池')


def build_research_data_tools(context):
    owner = str(context.get('user_id') or '')

    def invoke(operation):
        try:
            validate_owner(owner)
            if current_owner() and current_owner() != owner:
                raise ValueError('研究数据用户上下文不一致')
            with user_scope(owner):
                value = {'success': True, 'data': operation(ResearchDataService())}
        except ValidationError:
            value = {'success': False, 'message': '研究数据查询参数不符合目录或范围约束，请检查工具参数说明'}
        except ValueError as exc:
            value = {'success': False, 'message': str(exc)[:600]}
        except Exception:
            value = {'success': False, 'message': '本人研究数据库暂时不可用；未返回替代、演示或其他账号的数据'}
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False, default=str, separators=(',', ':'))
        if len(rendered) > 64000:
            return json.dumps({'success': False, 'message': '查询结果过大，请缩小 fields/tables/limit 后重试'}, ensure_ascii=False)
        return rendered

    def tables():
        return invoke(lambda service: service.list_factor_tables())

    def fields(tables, limit=40, offset=0):
        return invoke(lambda service: service.list_factor_fields(tables, limit=limit, offset=offset))

    def read(table, fields, date, security):
        return invoke(lambda service: service.read_factor_data(table, fields, date, security))

    def pool(as_of=None, codes=None, filters=None, groups=None, limit=100):
        normalize = lambda items: [item.model_dump() if hasattr(item, 'model_dump') else item for item in (items or [])]
        return invoke(lambda service: service.screen_stock_pool(as_of=as_of, codes=codes,
                      filters=normalize(filters), groups=normalize(groups), limit=limit))

    return [
        StructuredTool.from_function(tables, name='list_factor_tables', description='发现本人数据库可读表、真实覆盖区间与缺失来源。不会把源财务/历史行业或指数成分表假装已导入。'),
        StructuredTool.from_function(fields, name='list_factor_fields', args_schema=_FieldsArgs,
            description='分页列出目录表字段、表达式模板及 research_supported。自定义因子可以查值，但当前回测仅支持 Alpha158。'),
        StructuredTool.from_function(read, name='read_factor_data', args_schema=_ReadArgs,
            description='按目录字段、日期和股票读取本人真实数据。自定义因子只用该日及以前行情计算；缺数据如实返回，不执行任意 SQL。'),
        StructuredTool.from_function(pool, name='screen_stock_pool', args_schema=_PoolArgs,
            description='在本人已存 Alpha158 时点覆盖范围筛选股票代码和因子阈值。返回 codes/as_of/count/truncated，不生成假的 poolId；历史行业/指数成分不可用。不得用于早于 as_of 的回测起点。'),
    ]
