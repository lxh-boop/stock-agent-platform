"""Personal saved research strategies, backed by immutable report/version links."""
from __future__ import annotations

from application.backtest_service import BacktestService
from backtesting import strategy_store
from core.user_scope import current_owner


class StrategyService:
    @staticmethod
    def _task(task_id):
        from server.task_runtime.store import TaskStore
        from server.api.tasks import _public_task
        task = TaskStore().get(task_id)
        if task['owner_id'] != current_owner() or task['task_type'] != 'backtest.run':
            raise KeyError('任务不存在或不属于当前用户')
        return _public_task(task)

    @classmethod
    def _refresh(cls):
        for run in strategy_store.unfinished():
            try:
                task_id = run['task_id'] or strategy_store.recover_task_id(str(run['run_id']))
                if not task_id:
                    # Dispatch may have committed between the first lookup and
                    # acquiring this lock. Recheck under the same exclusion used
                    # by submit, and hold it until an empty reservation is closed.
                    with strategy_store.idle_dispatch() as acquired:
                        if not acquired:
                            continue
                        task_id = strategy_store.recover_task_id(str(run['run_id']))
                        if not task_id:
                            strategy_store.reconcile(str(run['run_id']), 'interrupted', require_unattached=True)
                            continue
                task = cls._task(task_id)
                if not run['task_id']:
                    strategy_store.attach_task(str(run['run_id']), task)
                status = task['status']
            except KeyError:
                status = 'interrupted'
            strategy_store.reconcile(str(run['run_id']), status)

    @classmethod
    def _with_task(cls, strategy):
        if strategy['last_task_id']:
            try:
                strategy['task'] = cls._task(strategy['last_task_id'])
            except KeyError:
                strategy['task'] = None
        return strategy

    @classmethod
    def list(cls, limit=30, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError('策略分页参数无效')
        cls._refresh()
        result = strategy_store.listing(limit, offset)
        result['records'] = [cls._with_task(item) for item in result['records']]
        return result

    @classmethod
    def detail(cls, strategy_id):
        cls._refresh()
        return cls._with_task(strategy_store.get(strategy_id))

    @classmethod
    def from_report(cls, run_id, *, request_id, name=None):
        ident = strategy_store.from_report(run_id, request_id=request_id, name=name)
        return cls.detail(ident)

    @classmethod
    def save(cls, strategy_id, spec, *, request_id, expected_version=None):
        cls._refresh()
        ident = strategy_store.save(strategy_id, spec, request_id=request_id, expected_version=expected_version, require_version=True)
        return cls.detail(ident)

    @classmethod
    def rerun(cls, strategy_id, *, request_id, expected_version=None, session_id=''):
        if expected_version is None:
            raise ValueError('请先读取策略当前版本，再确认重新回测')
        cls._refresh()
        return strategy_store.submit(strategy_id, request_id=request_id, expected_version=expected_version,
            callback=lambda spec: BacktestService.submit(spec, request_id=request_id, kind='backtest', session_id=session_id))

    @classmethod
    def delete(cls, strategy_id, *, request_id, expected_version=None):
        if expected_version is None:
            raise ValueError('请先读取策略当前版本，再确认删除')
        cls._refresh()
        strategy_store.delete(strategy_id, request_id=request_id, expected_version=expected_version)
        return {'deleted': True}
