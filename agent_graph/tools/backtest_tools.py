"""Agent和页面调用同一套真实研究服务；不加载旧演示引擎或另一个Node服务。"""
from typing import Annotated, Literal
from uuid import UUID, NAMESPACE_URL, uuid5
from pydantic import BaseModel, ConfigDict, Field
from langchain_core.tools import InjectedToolCallId, StructuredTool

from agent_graph.messages_utils import json_view
from backtesting.contracts import BacktestSpec, AnalysisSpec, ScreenSpec
from core.user_scope import current_owner, user_scope, validate_owner


def build_backtest_tools(context):
    owner = str(context.get('user_id') or '')
    session_id = str(context.get('session_id') or '')

    def request_id(tool_call_id, operation):
        # The model cannot choose this key. Checkpoint replay of the same tool
        # call must return the same task/strategy, even after a network failure.
        if not tool_call_id or not context.get('run_id') or not session_id:
            raise ValueError('研究操作缺少工具调用标识')
        return str(uuid5(NAMESPACE_URL, f"research:{owner}:{session_id}:{context['run_id']}:{tool_call_id}:{operation}"))

    def task_view(task):
        if task.get('owner_id') != owner or task.get('task_type') != 'backtest.run':
            raise KeyError('研究任务不存在或不属于当前用户')
        return {key: task.get(key) for key in ('task_id', 'status', 'progress', 'message', 'created_at', 'started_at', 'finished_at')}

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

    class WriteArgs(BaseModel):
        model_config = ConfigDict(extra='forbid')
        tool_call_id: Annotated[str, InjectedToolCallId]

    class RunArgs(WriteArgs):
        spec: BacktestSpec = Field(description='真实个人策略规格；先查目录和日期，支持定期/触发、IC权重、因子仓位、指数择时。')

    class AnalysisArgs(WriteArgs):
        spec: AnalysisSpec = Field(description='真实因子研究：分层、Rank IC/IR、IC衰减、因子相关矩阵。与含交易成本回测分开。')

    class ScreenArgs(WriteArgs):
        spec: ScreenSpec = Field(description='当日筛选；start_date=end_date=查询交易日，使用与回测相同的因子过滤、权重和排序。')

    def execute(spec, kind, tool_call_id):
        from application.backtest_service import BacktestService
        value = spec.model_dump(mode='json') if hasattr(spec, 'model_dump') else spec
        return invoke(lambda: task_view(BacktestService.submit(value, kind=kind,
            request_id=request_id(tool_call_id, kind), session_id=session_id)))

    def run(spec, tool_call_id): return execute(spec, 'backtest', tool_call_id)
    def analysis(spec, tool_call_id): return execute(spec, 'analysis', tool_call_id)
    def screen(spec, tool_call_id): return execute(spec, 'screen', tool_call_id)

    class ReadArgs(BaseModel):
        model_config = ConfigDict(extra='forbid')
        run_id: UUID | None = Field(default=None, description='留空列出本人研究历史；传UUID读取真实报告摘要')
        task_id: str | None = Field(default=None, max_length=100, description='执行工具返回的task_id；查询真实后台状态，成功后读取其报告。与run_id二选一。')
        row_kind: Literal['equity','trades','positions','skipped_orders','signals','periods',
            'ic_series','segments','long_short','ic_decay','correlation','selections'] | None = Field(default=None,
            description='读取报告明细。因子相关矩阵用correlation；IC序列ic_series、分层segments、衰减ic_decay；必须同时给run_id或task_id。')
        limit: int = Field(default=20, ge=1, le=20, description='每页明细数，最多20条；根据total/offset继续分页。')
        offset: int = Field(default=0, ge=0)

    def read(run_id=None, task_id=None, row_kind=None, limit=20, offset=0):
        from backtesting.store import get_report, list_reports, report_rows
        def detail(report_id):
            if row_kind:
                # Read only this page, not every historical holding/trade. The
                # store independently verifies report ownership in user scope.
                return {'run_id':str(report_id), 'row_kind':row_kind,
                        **report_rows(str(report_id), row_kind, limit, offset)}
            return get_report(str(report_id), summary=True)
        def result():
            if run_id and task_id:
                raise ValueError('请仅提供 run_id 或 task_id')
            if row_kind and not (run_id or task_id):
                raise ValueError('读取明细前请提供本人报告 run_id 或研究 task_id')
            if task_id:
                from server.api.tasks import task_manager
                task = task_manager.store.get(task_id)
                data = task_view(task)
                if task['status'] == 'succeeded':
                    report_id = (task.get('result') or {}).get('run_id')
                    if not report_id:
                        raise ValueError('任务未返回已保存的研究报告')
                    data['report'] = detail(report_id)
                    data['report_url'] = f'/strategies?report={report_id}'
                return data
            if run_id:
                return {**detail(run_id), 'report_url': f'/strategies?report={run_id}'}
            history = list_reports(10)
            return {'total': history['total'], 'records': [{
                **{key: item.get(key) for key in ('run_id', 'kind', 'created_at', 'metrics')},
                'name': item.get('spec', {}).get('name'),
                'start_date': item.get('spec', {}).get('start_date'),
                'end_date': item.get('spec', {}).get('end_date'),
            } for item in history['records']]}
        return invoke(result)

    class SaveArgs(WriteArgs):
        spec: BacktestSpec
        strategy_id: UUID | None = Field(default=None, description='新策略留空；编辑已保存策略时填本人策略ID')
        expected_version: int | None = Field(default=None, ge=1, description='编辑时必填策略列表返回的当前版本，防止审批后覆盖别人已修改的参数；新建留空。')

    def save(spec, tool_call_id, strategy_id=None, expected_version=None):
        from application.strategy_service import StrategyService
        value = spec.model_dump(mode='json') if hasattr(spec, 'model_dump') else spec
        def persist():
            if strategy_id and expected_version is None:
                raise ValueError('编辑策略前请读取当前版本，并填写 expected_version')
            if not strategy_id and expected_version is not None:
                raise ValueError('新策略不填写 expected_version')
            ident = request_id(tool_call_id, 'save-spec')
            saved = StrategyService.save(str(strategy_id or ident), value,
                request_id=ident, expected_version=expected_version)
            return strategy_view(saved)
        return invoke(persist)

    def strategy_view(item):
        # Keep list/tool receipts small and parseable; full parameters and report
        # rows remain in the strategy page and the explicit report read tool.
        report = item.get('latest_report') or {}
        return {**{key: item.get(key) for key in ('strategy_id', 'version', 'status', 'latest_run_id',
            'latest_run_version', 'active_task_id', 'last_task_id')},
            'name': item.get('spec', {}).get('name'),
            'metrics': report.get('metrics'),
            'strategy_url': f"/strategies?strategy={item['strategy_id']}"}

    class StrategyReadArgs(BaseModel):
        model_config = ConfigDict(extra='forbid')
        strategy_id: UUID | None = Field(default=None, description='留空分页列策略；传本人策略ID读取完整当前参数与版本，用于修改。')
        offset: int = Field(default=0, ge=0, description='列表分页起点，每页20条。')

    def strategies(strategy_id=None, offset=0):
        from application.strategy_service import StrategyService
        def listing():
            if strategy_id:
                item = StrategyService.detail(str(strategy_id))
                return {**strategy_view(item), 'spec': item['spec']}
            result = StrategyService.list(limit=20, offset=offset)
            return {**{key: result.get(key) for key in ('total', 'stats')},
                    'offset': offset, 'next_offset': offset + 20 if result['total'] > offset + 20 else None,
                    'records': [strategy_view(item) for item in result['records']]}
        return invoke(listing)

    class FromReportArgs(WriteArgs):
        run_id: UUID = Field(description='本人已完成的backtest报告UUID；不接受task_id或模型编造的结果。')
        name: str | None = Field(default=None, max_length=80)

    def from_report(run_id, tool_call_id, name=None):
        from application.strategy_service import StrategyService
        return invoke(lambda: strategy_view(StrategyService.from_report(str(run_id), name=name,
            request_id=request_id(tool_call_id, 'from-report'))))

    class StrategyArgs(WriteArgs):
        strategy_id: UUID
        expected_version: int = Field(ge=1, description='策略列表返回的当前版本；防止批准后参数被修改。')

    class RerunArgs(StrategyArgs):
        pass

    def rerun(strategy_id, expected_version, tool_call_id):
        from application.strategy_service import StrategyService
        return invoke(lambda: task_view(StrategyService.rerun(str(strategy_id),
            expected_version=expected_version, request_id=request_id(tool_call_id, 'rerun'), session_id=session_id)))

    def delete(strategy_id, expected_version, tool_call_id):
        from application.strategy_service import StrategyService
        return invoke(lambda: StrategyService.delete(str(strategy_id), expected_version=expected_version,
            request_id=request_id(tool_call_id, 'delete')))

    return [
        StructuredTool.from_function(catalog, name='backtest_catalog', description='查询本人Alpha158字段、指数目录和数据范围。没有数据先做个人日更；不能把其它仓库财务字段猜成Alpha158。'),
        StructuredTool.from_function(run, name='run_strategy_backtest', args_schema=RunArgs,
            description='用户批准后提交真实个人策略后台回测，返回task_id而非收益；用get_strategy_backtest查询完成后的报告。不修改模拟盘。缺参数先澄清。'),
        StructuredTool.from_function(analysis, name='analyze_factors', args_schema=AnalysisArgs,
            description='用户批准后提交本人因子分析后台任务，返回task_id；分析分层收益、Rank IC/IR、IC衰减与相关矩阵。'),
        StructuredTool.from_function(screen, name='screen_factor_strategy', args_schema=ScreenArgs,
            description='用户批准后提交指定日因子筛选后台任务，返回task_id，不下单。start_date=end_date。'),
        StructuredTool.from_function(read, name='get_strategy_backtest', args_schema=ReadArgs,
            description='通过task_id查询本人研究任务状态和成功报告；或按run_id读取报告、留空查历史。row_kind可分页读取实际相关矩阵、IC/分层/衰减、持仓与成交，不编造未读取的数值。尚未完成不能宣称已有收益。'),
        StructuredTool.from_function(save, name='save_factor_strategy', args_schema=SaveArgs,
            description='仅在用户明确要求保存/修改策略时调用。保存本人策略规格供页面重新编辑回测，不启用自动交易。'),
        StructuredTool.from_function(strategies, name='list_factor_strategies', args_schema=StrategyReadArgs,
            description='分页列出本人策略工作台；传strategy_id读取完整参数与当前版本。编辑、重跑和删除需使用返回的ID与版本。'),
        StructuredTool.from_function(from_report, name='save_strategy_from_report', args_schema=FromReportArgs,
            description='用户批准后把本人已完成的真实回测报告保存为策略，保留参数版本与报告关联；不再次回测。'),
        StructuredTool.from_function(rerun, name='rerun_factor_strategy', args_schema=RerunArgs,
            description='用户批准后按本人保存策略的指定版本提交后台回测，返回task_id。先查询策略列表，不猜ID或版本。'),
        StructuredTool.from_function(delete, name='delete_factor_strategy', args_schema=StrategyArgs,
            description='用户明确要求删除且批准后删除本人策略；先核对ID，不删除研究数据或模拟盘账户。'),
    ]
