import { describe, expect, it } from 'vitest'
import { strategyDraftPrompt, strategyFactorLabel, strategyPercent } from '../utils/strategies'
import type { BacktestCatalog, BacktestSpec } from '../types/backtests'
import type { NewStrategyDraft } from '../types/strategies'

const catalog: BacktestCatalog = { fields: ['KMID', 'MA5'], message: '', max_stocks: 1000, max_positions: 50,
  indexes: {}, index_fields: [], schemas: {} as BacktestCatalog['schemas'],
  coverage: { start_date: '2025-01-02', end_date: '2026-11-17', stock_count: 300, feature_version: 'test' } }
const draft: NewStrategyDraft = { name: '我的策略', start_date: '2025-01-02', end_date: '2026-11-17', initial_cash: 100000,
  rebalance_days: 5, max_positions: 10, benchmark: 'pool', factors: [{ field: 'KMID', direction: -1 }] }

describe('strategy workspace data conventions', () => {
  it('formats fractional production returns as percentages without fake missing zeros', () => {
    expect(strategyPercent(.1234)).toBe('12.34%')
    expect(strategyPercent(-.08)).toBe('-8.00%')
    expect(strategyPercent(null)).toBe('—')
    expect(strategyPercent(Number.NaN)).toBe('—')
  })
  it('keeps factor direction and distinguishes dynamic weights from fixed proportions', () => {
    const spec = { factors: [{ field: 'KMID', direction: -1, weight: 2 }, { field: 'MA5', direction: 1, weight: 1 }] } as BacktestSpec
    expect(strategyFactorLabel(spec, 0)).toBe('KMID · 值小优先 · 67%')
    expect(strategyFactorLabel({ ...spec, weighting: { method: 'rank_ic', window: 20 } }, 0)).toBe('KMID · 值小优先 · 动态权重')
  })
  it('builds an explicit draft using owned coverage, exact dates, factor directions and no execution command', () => {
    const prompt = strategyDraftPrompt(draft, catalog)
    expect(prompt).toContain('2025-01-02 至 2026-11-17')
    expect(prompt).toContain('KMID（值小优先）')
    expect(prompt).toContain('100000 元')
    expect(prompt).toContain('得到确认后运行回测')
    expect(prompt).toContain('不修改已有模拟账户')
  })
  it('refuses missing data, future dates, unknown or duplicate factors instead of silently substituting', () => {
    expect(() => strategyDraftPrompt(draft, { ...catalog, coverage: null })).toThrow('暂无')
    expect(() => strategyDraftPrompt({ ...draft, end_date: '2026-11-18' }, catalog)).toThrow('数据范围')
    expect(() => strategyDraftPrompt({ ...draft, factors: [{ field: 'UNKNOWN', direction: 1 }] }, catalog)).toThrow('当前可用')
    expect(() => strategyDraftPrompt({ ...draft, factors: [...draft.factors, ...draft.factors] }, catalog)).toThrow('重复')
  })
})
