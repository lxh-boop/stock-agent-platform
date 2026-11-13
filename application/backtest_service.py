"""回测唯一应用入口；页面走 Task Worker，Agent 在现有 Agent Worker 内调用相同入口。"""
from __future__ import annotations

import hashlib
import json
from typing import Callable
from uuid import UUID, uuid4

from backtesting.contracts import BacktestSpec, SPEC_TYPES, ROW_KINDS
from backtesting.engine import simulate
from backtesting.analysis import analyze, screen
from backtesting.market import prepare as prepare_market
from backtesting import store
from core.user_scope import current_owner


class BacktestService:
    @staticmethod
    def run(spec: dict, *, run_id: str | None = None, kind: str = 'backtest',
            on_progress: Callable = lambda _: None, cancelled: Callable = lambda: False) -> dict:
        if not current_owner():
            raise ValueError('请先登录')
        if kind not in SPEC_TYPES:
            raise ValueError('未知研究任务类型')
        parsed = SPEC_TYPES[kind].model_validate(spec)
        ident = str(UUID(run_id)) if run_id else str(uuid4())
        if run_id:
            try:
                existing = store.get_report(ident, summary=True)
            except KeyError:
                existing = None
            if existing is not None:
                if existing['kind'] != kind or existing['spec'] != parsed.model_dump(mode='json'):
                    raise ValueError('运行编号已用于不同参数')
                return existing
        if cancelled():
            raise InterruptedError('研究任务已取消')
        with store.execution_lock():
            prepare_market(parsed, cancelled=cancelled)
            with store.snapshot(parsed) as (meta, days):
                metadata = dict(meta)
                markets = metadata.pop('_markets', {})
                engine = {'backtest': simulate, 'analysis': analyze, 'screen': screen}[kind]
                report = engine(parsed, days, markets=markets, on_progress=on_progress, cancelled=cancelled)
            if cancelled():
                raise InterruptedError('研究任务已取消')
            report['data_snapshot'] = metadata
            report['run_id'] = ident
            store.save_report(ident, report)
        # Task/Agent只返回摘要；完整明细经数据库分页读取，不截断后50笔。
        return {k: v for k, v in report.items() if k not in ROW_KINDS}

    @staticmethod
    def submit(spec: dict, *, request_id: str, kind: str = 'backtest') -> dict:
        """同用户、同请求UUID重复提交返回同一任务；不能把UUID用于不同参数。"""
        from database.connection import get_connection
        from server.api.tasks import task_manager
        owner = current_owner()
        if not owner:
            raise ValueError('请先登录')
        if kind not in SPEC_TYPES:
            raise ValueError('未知研究任务类型')
        parsed = SPEC_TYPES[kind].model_validate(spec).model_dump(mode='json')
        ident = str(UUID(request_id))
        hashed = hashlib.sha256(json.dumps([kind, parsed], sort_keys=True).encode()).hexdigest()
        # session advisory锁跨进程有效；task_manager采用自己的连接写任务，不能用事务行锁代替。
        with get_connection(runtime=True) as conn:
            conn.execute('SELECT pg_advisory_lock(hashtextextended(%s, 0))', ('backtest-submit:' + owner,))
            conn.commit()
            # 初始化runtime表发生在查询之前，不读取或暴露其他账号任务。
            tasks = task_manager.store
            row = conn.execute("""SELECT task_id, metadata_json FROM task_runs WHERE owner_id=%s
                AND task_type='backtest.run' AND metadata_json::jsonb->>'request_id'=%s
                ORDER BY created_at DESC LIMIT 1""", (owner, ident)).fetchone()
            if row:
                meta = json.loads(row['metadata_json'])
                if meta.get('spec_hash') != hashed:
                    raise ValueError('此请求编号已用于不同回测参数，请创建新请求')
                return tasks.get(row['task_id'])
            active = tasks.list(owner_id=owner, task_type='backtest.run', active_only=True, limit=1)
            if active:
                raise ValueError('本人已有回测正在运行，请先完成或取消它')
            return task_manager.submit(task_type='backtest.run', owner_id=owner,
                kwargs={'spec': parsed, 'run_id': ident, 'kind': kind},
                metadata={'request_id': ident, 'spec_hash': hashed, 'scope': 'account', 'kind': kind},
                timeout_seconds=3600, max_retries=0)
