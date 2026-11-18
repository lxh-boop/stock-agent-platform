"""Factor library use cases; read and preview only the authenticated user's data."""
from __future__ import annotations

import math
import re

from database.repositories.factor_catalog_repository import FactorCatalogRepository

_NAME = re.compile(r'^[\w\u3400-\u9fff][\w\u3400-\u9fff .-]{0,79}$', re.UNICODE)


class FactorCatalogService:
    def __init__(self, repository: FactorCatalogRepository | None = None) -> None:
        self.repository = repository or FactorCatalogRepository()

    def catalog(self) -> dict:
        from core.factor_expressions import expression_syntax, expression_templates
        items = self.repository.list()
        return {'items': items, 'total': len(items), 'storage': 'postgresql',
                'system_count': sum(item['source'] == 'alpha158' for item in items),
                'custom_count': sum(item['source'] == 'custom' for item in items),
                'templates': expression_templates(), 'syntax': expression_syntax(),
                'limits': {'preview_stocks': 3, 'preview_rows': 30}}

    @staticmethod
    def _validated(name: str, expression: str) -> dict:
        from core.factor_expressions import validate_expression
        name = name.strip()
        if not _NAME.fullmatch(name):
            raise ValueError('因子名称需为 1～80 个中文、字母、数字、空格、下划线、点或短横线')
        return {'name': name, **validate_expression(expression)}

    def validate(self, name: str, expression: str) -> dict:
        validated = self._validated(name, expression)
        return {'valid': True, **validated,
                'preview': self._preview(validated['expression'], factor_id='', limit=30)}

    def create(self, *, name: str, expression: str, confirmed: bool,
               request_id: str, idempotency_key: str) -> dict:
        if not confirmed:
            raise ValueError('请先确认因子名称和表达式再保存')
        if not request_id.strip() or not idempotency_key.strip():
            raise ValueError('提交标识不能为空，请重新提交')
        # Revalidate the final payload; a successful preview never authorizes changed code.
        validated = self._validated(name, expression)
        return self.repository.create(validated['name'], validated['expression'],
                                      request_id=request_id, idempotency_key=idempotency_key)

    def values(self, factor_id: str, *, limit: int = 30, code: str | None = None) -> dict:
        factor = self.repository.get(factor_id)
        if factor['source'] == 'custom':
            return self._preview(factor['expression'], factor_id=factor_id, limit=limit, code=code)
        values = self.repository.stored_values(factor['name'], limit=limit, code=code)
        status = 'ready' if values['available_values'] else ('insufficient_history' if values['total'] else 'empty')
        messages = {'ready': '读取当前账号数据库中已保存的 Alpha158 因子值。',
                    'empty': '当前账号还没有该因子的已存数值，请先完成一次日更。',
                    'insufficient_history': '已有行情，但该因子的历史窗口尚未满足，暂无有效数值。'}
        return {'factor_id': factor_id, **values, 'source': 'stored', 'status': status,
                'message': messages[status]}

    def _preview(self, expression: str, *, factor_id: str, limit: int,
                 code: str | None = None) -> dict:
        from core.factor_expressions import evaluate_expression
        frame = self.repository.preview_prices(code=code)
        result = {'factor_id': factor_id, 'items': [], 'total': 0, 'available_values': 0,
                  'as_of': None, 'source': 'computed', 'status': 'empty',
                  'message': '当前账号还没有可用于计算的已存行情，请先完成一次日更。'}
        if frame.empty:
            return result
        rows = []
        # Never compute a rolling window across stock boundaries or allow future leakage.
        for stock_code, group in frame.groupby('code', sort=True):
            group = group.sort_values('date').reset_index(drop=True)
            values = evaluate_expression(expression, group)
            for day, value in zip(group['date'], values, strict=True):
                number = float(value) if value is not None and math.isfinite(value) else None
                rows.append({'code': str(stock_code), 'date': str(day)[:10], 'value': number})
        rows.sort(key=lambda row: row['code'])
        rows.sort(key=lambda row: row['date'], reverse=True)
        valid = sum(row['value'] is not None for row in rows)
        return {**result, 'items': rows[:limit], 'total': len(rows), 'available_values': valid,
                'as_of': rows[0]['date'], 'status': 'ready' if valid else 'insufficient_history',
                'message': ('基于当前账号数据库中的最近行情即时计算；最多预览 3 只股票、每只 600 条行情，不写入历史因子值。'
                            if valid else '表达式有效，但现有行情不足以满足回看窗口或计算结果为空。')}
