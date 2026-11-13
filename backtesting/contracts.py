"""页面、后台任务、Agent 共用规格；不保留旧 Strategy/MySQL/latest 参数。"""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from core.alpha158_contract import FEATURE_COLUMNS

ENGINE_VERSION = 'factor-event-v2'
DISCLAIMER = '本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。'
MAX_STOCKS, MAX_DAYS, MAX_TRADES = 1000, 1300, 20000
INDEX_CODES = {'hs300': '000300.SH', 'csi500': '000905.SH', 'sse': '000001.SH'}
INDEX_FIELDS = ('open', 'high', 'low', 'close', 'pre_close', 'pct_chg', 'volume', 'amount',
                'ma5', 'ma20', 'ma60', 'close_ma20', 'ret5', 'ret20', 'vol20', 'drawdown20')
ROW_KINDS = ('equity', 'trades', 'positions', 'skipped_orders', 'signals', 'periods',
             'ic_series', 'segments', 'long_short', 'ic_decay', 'correlation', 'selections')


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, validate_default=True)


class Factor(StrictModel):
    field: str
    direction: Literal[1, -1] = 1
    weight: float = Field(default=1, ge=0, le=100)

    @field_validator('field')
    @classmethod
    def known_field(cls, value: str) -> str:
        if value not in FEATURE_COLUMNS:
            raise ValueError('只能使用个人 Alpha158 因子目录中的字段')
        return value


class Predicate(StrictModel):
    field: str
    op: Literal['gt', 'lt', 'ge', 'le', 'eq', 'between']
    value: float
    upper: float | None = None

    @model_validator(mode='after')
    def valid_bounds(self):
        if self.op == 'between':
            if self.upper is None or self.upper < self.value:
                raise ValueError('区间条件必须提供不小于下限的 upper')
        elif self.upper is not None:
            raise ValueError('只有 between 条件可以填写 upper')
        return self


class Condition(Predicate):
    _known_field = field_validator('field')(Factor.known_field.__func__)


class MarketCondition(Predicate):
    index: Literal['hs300', 'csi500', 'sse'] = 'sse'

    @field_validator('field')
    @classmethod
    def field_exists(cls, value):
        if value not in INDEX_FIELDS:
            raise ValueError('指数字段不在目录中')
        return value


class Timing(StrictModel):
    rules: list[MarketCondition] = Field(min_length=1, max_length=3)
    enter_votes: int = Field(default=-1, ge=-1, le=3, strict=True)
    exit_votes: int = Field(default=-1, ge=-1, le=3, strict=True)
    bear_exposure: float = Field(default=0, ge=0, le=1)

    @model_validator(mode='after')
    def vote_counts(self):
        if any(n == 0 or n > len(self.rules) for n in (self.enter_votes, self.exit_votes)):
            raise ValueError('投票数为 -1（全部）或 1 至规则数')
        return self


class Weighting(StrictModel):
    method: Literal['fixed', 'rank_ic'] = 'fixed'
    window: int = Field(default=20, ge=5, le=250, strict=True)


class Sizing(StrictModel):
    method: Literal['equal', 'fixed', 'factor'] = 'equal'
    proportions: list[float] = Field(default_factory=list, max_length=50)
    field: str | None = None

    @model_validator(mode='after')
    def method_inputs(self):
        if self.method == 'factor':
            Factor.known_field(self.field or '')
        elif self.field is not None:
            raise ValueError('只有 factor 仓位函数使用 field')
        if any(v < 0 for v in self.proportions):
            raise ValueError('仓位比例必须非负')
        if self.method != 'fixed' and self.proportions:
            raise ValueError('只有 fixed 仓位函数使用 proportions')
        if self.method == 'fixed' and (not self.proportions or sum(self.proportions) <= 0):
            raise ValueError('固定仓位需要至少一个正比例')
        return self


def check_codes(values):
    if values is None:
        return None
    if not values or len(values) > MAX_STOCKS or any(len(c) != 6 or not c.isascii() or not c.isdigit() for c in values):
        raise ValueError(f'股票池必须为非空六位证券代码列表，上限 {MAX_STOCKS} 只')
    if len(values) != len(set(values)):
        raise ValueError('股票代码不能重复')
    return sorted(values)


class PoolSnapshot(StrictModel):
    as_of: date
    stock_codes: list[str] = Field(min_length=1, max_length=MAX_STOCKS)
    _codes = field_validator('stock_codes')(check_codes)


class DateRange(StrictModel):
    start: date
    end: date

    @model_validator(mode='after')
    def order(self):
        if self.end < self.start:
            raise ValueError('暂停区间结束日早于开始日')
        return self


class DataSpec(StrictModel):
    name: str = Field(default='因子策略', min_length=1, max_length=80)
    start_date: date
    end_date: date
    factors: list[Factor] = Field(min_length=1, max_length=10)
    stock_codes: list[str] | None = Field(default=None, max_length=MAX_STOCKS)
    # 用户提供的历史成员快照，只使用 as_of<=信号日 的最后一份，不冒充供应商历史成分。
    pool_schedule: list[PoolSnapshot] = Field(default_factory=list, max_length=120)
    _codes = field_validator('stock_codes')(check_codes)

    @model_validator(mode='after')
    def valid_data_spec(self):
        if self.end_date < self.start_date:
            raise ValueError('结束日期不能早于开始日期')
        if len({f.field for f in self.factors}) != len(self.factors):
            raise ValueError('排序因子不能重复')
        if sum(f.weight for f in self.factors) <= 0:
            raise ValueError('因子权重合计必须大于零')
        if self.pool_schedule:
            dates = [s.as_of for s in self.pool_schedule]
            if dates != sorted(set(dates)) or dates[0] > self.start_date:
                raise ValueError('历史股票池日期必须严格递增，且首份快照不晚于开始日')
            codes = {c for s in self.pool_schedule for c in s.stock_codes}
            if len(codes) > MAX_STOCKS:
                raise ValueError(f'历史股票池并集不能超过 {MAX_STOCKS} 只')
            if self.stock_codes and not codes <= set(self.stock_codes):
                raise ValueError('历史股票池成员必须包含在所选股票范围内')
        return self

    def fields(self) -> list[str]:
        return sorted({f.field for f in self.factors})

    def universe(self) -> list[str] | None:
        if self.stock_codes is not None:
            return self.stock_codes
        return sorted({c for s in self.pool_schedule for c in s.stock_codes}) if self.pool_schedule else None

    def indexes(self) -> set[str]:
        return set()

    def warmup_days(self) -> int:
        return 1


class BacktestSpec(DataSpec):
    entry_filters: list[Condition] = Field(default_factory=list, max_length=10)
    keep_rules: list[Condition] = Field(default_factory=list, max_length=5)
    sell_filters: list[Condition] = Field(default_factory=list, max_length=5)
    market_filters: list[MarketCondition] = Field(default_factory=list, max_length=5)
    weighting: Weighting = Field(default_factory=Weighting)
    sizing: Sizing = Field(default_factory=Sizing)
    timing: Timing | None = None
    benchmark: Literal['pool', 'hs300', 'csi500', 'sse'] = 'pool'
    initial_cash: float = Field(default=1_000_000, ge=10_000, le=1_000_000_000)
    max_positions: int = Field(default=10, ge=1, le=50, strict=True)
    rebalance_days: int = Field(default=5, ge=0, le=250, strict=True)
    exposure: float = Field(default=1, gt=0, le=1)
    min_position: float = Field(default=0, ge=0, le=1)
    position_band: float = Field(default=.05, ge=0, le=.5)
    keep_invested: bool = False
    execution: Literal['open', 'close'] = 'open'
    buy_fee: float = Field(default=.0003, ge=0, le=.02)
    sell_fee: float = Field(default=.0008, ge=0, le=.02)
    slippage_bps: float = Field(default=0, ge=0, le=100)
    # 研究型固定比例限制，非按板块/ST/上市日自动匹配的真实涨跌停。
    limit_pct: float | None = Field(default=None, gt=0, le=.5)
    stop_loss: float | None = Field(default=None, gt=0, lt=1)
    take_profit: float | None = Field(default=None, gt=0, le=10)
    trailing_stop: float | None = Field(default=None, gt=0, lt=1)
    max_hold_days: int | None = Field(default=None, ge=1, le=1300, strict=True)
    rank_out: int | None = Field(default=None, ge=1, le=MAX_STOCKS, strict=True)
    exclude_ranges: list[DateRange] = Field(default_factory=list, max_length=5)

    @model_validator(mode='after')
    def valid_spec(self):
        if self.end_date <= self.start_date and not isinstance(self, ScreenSpec):
            raise ValueError('结束日期必须晚于开始日期')
        if self.rebalance_days == 0 and not self.entry_filters:
            raise ValueError('条件触发模式至少需要一个买入条件')
        if len(self.sizing.proportions) > self.max_positions:
            raise ValueError('固定仓位比例数量不能超过最多持仓数')
        ranges = sorted(self.exclude_ranges, key=lambda r: r.start)
        if any(a.end >= b.start for a, b in zip(ranges, ranges[1:])):
            raise ValueError('暂停交易区间不能重叠')
        return self

    def fields(self) -> list[str]:
        fields = {x.field for x in [*self.factors, *self.entry_filters, *self.keep_rules, *self.sell_filters]}
        if self.sizing.field:
            fields.add(self.sizing.field)
        return sorted(fields)

    def indexes(self) -> set[str]:
        return ({self.benchmark} if self.benchmark != 'pool' else set()) | {
            r.index for r in [*self.market_filters, *(self.timing.rules if self.timing else [])]}

    def warmup_days(self) -> int:
        return self.weighting.window + 1 if self.weighting.method == 'rank_ic' else 1


class AnalysisSpec(DataSpec):
    segment_count: int = Field(default=5, ge=2, le=10, strict=True)
    interval_days: int = Field(default=5, ge=1, le=250, strict=True)
    decay_days: int = Field(default=5, ge=1, le=20, strict=True)

    @model_validator(mode='after')
    def enough_range(self):
        if self.end_date <= self.start_date:
            raise ValueError('因子分析至少需要两个交易日')
        return self


class ScreenSpec(BacktestSpec):
    @model_validator(mode='after')
    def single_day(self):
        if self.start_date != self.end_date:
            raise ValueError('每日筛选的开始和结束日期必须相同')
        return self


class BacktestRequest(StrictModel):
    spec: BacktestSpec
    request_id: UUID
    confirmed: Literal[True]


class AnalysisRequest(StrictModel):
    spec: AnalysisSpec
    request_id: UUID
    confirmed: Literal[True]


class ScreenRequest(StrictModel):
    spec: ScreenSpec
    request_id: UUID
    confirmed: Literal[True]


class StrategyRequest(StrictModel):
    spec: BacktestSpec
    strategy_id: UUID
    confirmed: Literal[True]


class DeleteRequest(StrictModel):
    confirmed: Literal[True]


SPEC_TYPES = {'backtest': BacktestSpec, 'analysis': AnalysisSpec, 'screen': ScreenSpec}
