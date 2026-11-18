import type { BacktestCatalog, BacktestSpec } from '../types/backtests'
import type { NewStrategyDraft, StrategyWorkspaceItem } from '../types/strategies'
import { TERMINAL_TASK_STATUSES } from '../types/task'

export const benchmarkLabels = { pool: '本人股票池等权参考', hs300: '沪深300指数', csi500: '中证500指数', sse: '上证指数' }

export function strategyPercent(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '—'
}

export function strategyNumber(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—'
}

export function strategyIsRunning(strategy: StrategyWorkspaceItem): boolean {
  return Boolean(strategy.active_task_id) || (strategy.status !== 'draft' && !TERMINAL_TASK_STATUSES.has(strategy.status))
}

export function strategyFactorLabel(spec: BacktestSpec, index: number): string {
  const factor = spec.factors[index]
  const direction = factor.direction === 1 ? '值大优先' : '值小优先'
  if (spec.weighting?.method === 'rank_ic') return `${factor.field} · ${direction} · 动态权重`
  const total = spec.factors.reduce((sum, item) => sum + item.weight, 0)
  return `${factor.field} · ${direction}${total > 0 ? ` · ${(factor.weight / total * 100).toFixed(0)}%` : ''}`
}

export function strategyDraftPrompt(draft: NewStrategyDraft, catalog: BacktestCatalog): string {
  const coverage = catalog.coverage
  if (!coverage) throw new Error('本人因子库暂无可用数据，请先完成日更。')
  if (!draft.name.trim()) throw new Error('请填写策略名称。')
  if (!draft.start_date || !draft.end_date || draft.start_date >= draft.end_date) throw new Error('开始日期必须早于结束日期。')
  if (draft.start_date < coverage.start_date || draft.end_date > coverage.end_date) {
    throw new Error(`日期须在本人数据范围内：${coverage.start_date} 至 ${coverage.end_date}。`)
  }
  if (!draft.factors.length || draft.factors.length > 10 || draft.factors.some(f => !catalog.fields.includes(f.field))) {
    throw new Error('请选择 1～10 个当前可用因子。')
  }
  if (new Set(draft.factors.map(f => f.field)).size !== draft.factors.length) throw new Error('请勿重复选择同一个因子。')
  if (!Number.isFinite(draft.initial_cash) || draft.initial_cash < 10000 || draft.initial_cash > 1e9) throw new Error('初始资金须为 1 万至 10 亿元。')
  if (!Number.isInteger(draft.max_positions) || draft.max_positions < 1 || draft.max_positions > catalog.max_positions) throw new Error('持仓数量超出可用范围。')
  if (!Number.isInteger(draft.rebalance_days) || draft.rebalance_days < 1 || draft.rebalance_days > 250) throw new Error('调仓间隔须为 1～250 个交易日。')
  if (!(draft.benchmark in benchmarkLabels)) throw new Error('请选择可用参考基准。')
  const factors = draft.factors.map(f => `${f.field}（${f.direction === 1 ? '值大优先' : '值小优先'}）`).join('、')
  return `请帮我构建多因子策略「${draft.name.trim()}」。\n` +
    `股票范围：仅使用我本人数据库中可用的股票，不当作历史指数成分股。\n` +
    `因子：${factors}，初始采用固定等权因子评分。\n` +
    `回测区间：${draft.start_date} 至 ${draft.end_date}；初始资金：${draft.initial_cash} 元；` +
    `每 ${draft.rebalance_days} 个交易日调仓，最多持有 ${draft.max_positions} 只股票；参考基准：${benchmarkLabels[draft.benchmark]}。\n` +
    `请先读取我的可用数据与参数，列出拟采用的成交时点、费用和仓位假设，得到确认后运行回测并展示真实报告。` +
    `若参数或数据不足，请说明，不要静默改变日期或使用示例数据。回测完成后询问是否保存为我的策略，不修改已有模拟账户。`
}
