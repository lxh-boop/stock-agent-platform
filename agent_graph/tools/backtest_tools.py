"""Agent和页面调用同一套真实研究服务；不加载旧演示引擎或另一个Node服务。"""
from uuid import UUID, uuid4
from pydantic import BaseModel, ConfigDict, Field
from langchain_core.tools import StructuredTool

from agent_graph.messages_utils import json_view
from backtesting.contracts import BacktestSpec, AnalysisSpec, ScreenSpec, ROW_KINDS
from core.user_scope import current_owner, user_scope, validate_owner


def build_backtest_tools(context):
    owner = str(context.get('user_id') or '')

    def invoke(fn):
        try:
            validate_owner(owner)
            if current_owner() and current_owner() != owner:
                raise ValueError('回测用户上下文不一致')
            with user_scope(owner):
                return json_view({'success': True, 'data': fn()}, limit=22000)
        except (ValueError, KeyError) as exc:
            return json_view({'success': False, 'message': str(exc)[:1200]})
        except Exception:
            return json_view({'success': False, 'message': '研究服务失败，请检查本人数据源或任务错误；没有生成演示结果。'})

    def catalog():
        from backtesting.store import catalog as read
        return invoke(lambda: {k: v for k, v in read().items() if k != 'schemas'})

    class RunArgs(BaseModel):
        spec: BacktestSpec = Field(description='真实个人策略规格；先查目录和日期，支持定期/触发、IC权重、因子仓位、指数择时。')

    class AnalysisArgs(BaseModel):
        spec: AnalysisSpec = Field(description='真实因子研究：分层、Rank IC/IR、IC衰减、因子相关矩阵。与含交易成本回测分开。')

    class ScreenArgs(BaseModel):
        spec: ScreenSpec = Field(description='当日筛选；start_date=end_date=查询交易日，使用与回测相同的因子过滤、权重和排序。')

    def execute(spec, kind):
        from application.backtest_service import BacktestService
        value = spec.model_dump(mode='json') if hasattr(spec, 'model_dump') else spec
        return invoke(lambda: BacktestService.run(value, kind=kind))

    def run(spec): return execute(spec, 'backtest')
    def analysis(spec): return execute(spec, 'analysis')
    def screen(spec): return execute(spec, 'screen')

    class ReadArgs(BaseModel):
        run_id: UUID | None = Field(default=None, description='留空列出本人研究历史；传UUID读取真实报告摘要')

    def read(run_id=None):
        from backtesting.store import get_report, list_reports
        return invoke(lambda: get_report(str(run_id), summary=True) if run_id else list_reports(10))

    class SaveArgs(BaseModel):
        spec: BacktestSpec
        strategy_id: UUID | None = Field(default=None, description='新策略留空；编辑已保存策略时填本人策略ID')

    def save(spec, strategy_id=None):
        from backtesting.store import save_strategy
        value = spec.model_dump(mode='json') if hasattr(spec, 'model_dump') else spec
        return invoke(lambda: save_strategy(str(strategy_id or uuid4()), value))

    def strategies():
        from backtesting.store import list_strategies
        return invoke(lambda: list_strategies(20))

    return [
        StructuredTool.from_function(catalog, name='backtest_catalog', description='查询本人Alpha158字段、指数目录和数据范围。没有数据先做个人日更；不能把其它仓库财务字段猜成Alpha158。'),
        StructuredTool.from_function(run, name='run_strategy_backtest', args_schema=RunArgs,
            description='用户明确要求后执行真实个人策略回测；生成完整研究报告，不动模拟盘。动态权重/仓位/择时同页面契约。缺参数先澄清。'),
        StructuredTool.from_function(analysis, name='analyze_factors', args_schema=AnalysisArgs,
            description='用户要求后分析本人因子的分层收益、Rank IC/IR、1~N日IC衰减与相关矩阵。研究收益不计费用，不冒充可执行回测。'),
        StructuredTool.from_function(screen, name='screen_factor_strategy', args_schema=ScreenArgs,
            description='按指定日因子筛选排序并给目标权重，不下单。start_date=end_date；共用真实回测信号逻辑。'),
        StructuredTool.from_function(read, name='get_strategy_backtest', args_schema=ReadArgs,
            description='列出或读取本人真实回测/因子分析/每日选股报告。只引用已保存结果。'),
        StructuredTool.from_function(save, name='save_factor_strategy', args_schema=SaveArgs,
            description='仅在用户明确要求保存/修改策略时调用。保存本人策略规格供页面重新编辑回测，不启用自动交易。'),
        StructuredTool.from_function(strategies, name='list_factor_strategies', description='列出本人保存的因子策略，取得ID和参数后可再回测。'),
    ]
