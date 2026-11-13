import { describe, expect, it, vi } from 'vitest'
import { toBacktestSpec, toStudySpec, fromStudySpec, backtestError, backtestRequestId, type BacktestForm } from '../utils/backtest'

const form: BacktestForm = { name: 'test', start_date: '2026-01-01', end_date: '2026-02-01',
  factors: [{ field: 'KMID', direction: 1, weight: 1 }], initial_cash: 10000, max_positions: 1,
  rebalance_days: 5, exposure: 1, execution: 'open', buy_fee: .0003, sell_fee: .0008, slippage_bps: 0 }

describe('strategy backtest payload', () => {
  it('keeps leading zeros and supports Chinese comma', () => {
    const spec = toBacktestSpec({ ...form, codes: '000001，600000 000002' })
    expect(spec.stock_codes).toEqual(['000001', '600000', '000002'])
    expect(spec).not.toHaveProperty('codes')
    expect(spec).not.toHaveProperty('user_id')
  })
  it('empty stock list requests own dataset, never a global fallback', () => {
    expect(toBacktestSpec(form).stock_codes).toBeNull()
  })
  it('removes stale upper bounds when comparison operator changes', () => {
    const spec = toBacktestSpec({ ...form, entry_filters: [{ field: 'KMID', op: 'gt', value: 1, upper: 2 }] })
    expect(spec.entry_filters?.[0]).not.toHaveProperty('upper')
  })
  it('shows backend data failures instead of generic network errors', () => {
    expect(backtestError({ response: { data: { detail: '个人因子库为空' } } })).toBe('个人因子库为空')
    expect(backtestError({ response: { data: { detail: [{ msg: '参数非法' }] } } })).toBe('参数非法')
  })
})

it('creates UUID v4 without randomUUID on an HTTP page', () => {
  vi.stubGlobal('crypto', { getRandomValues: (values: Uint8Array) => values.fill(5) })
  try { expect(backtestRequestId()).toBe('05050505-0505-4505-8505-050505050505') }
  finally { vi.unstubAllGlobals() }
})


it('builds analysis-only payload without silently retaining trading parameters', () => {
  const value=toStudySpec({...form,segment_count:4,interval_days:3,decay_days:6},'analysis')
  expect(value).toHaveProperty('segment_count',4)
  expect(value).not.toHaveProperty('initial_cash')
  expect(value).not.toHaveProperty('buy_fee')
})

it('screen uses one explicit query date and never submits historical trading interval', () => {
  const value=toStudySpec(form,'screen')
  expect(value.start_date).toBe(form.end_date)
})

it('parses sizing and historical memberships, removing disabled timing', () => {
  const value=toBacktestSpec({...form,sizing:{method:'fixed',proportions:[]},proportions_text:'0.5, 0.3, 0.2',
    pool_schedule_text:'[{"as_of":"2026-01-01","stock_codes":["000001"]}]',timing_enabled:false,
    timing:{rules:[{field:'close',op:'gt',value:100,index:'sse'}],enter_votes:1,exit_votes:1,bear_exposure:0}})
  expect(value.sizing?.proportions).toEqual([.5,.3,.2])
  expect(value.pool_schedule?.[0].stock_codes).toEqual(['000001'])
  expect(value.timing).toBeNull()
  expect(value).not.toHaveProperty('proportions_text')
})

it('rejects malformed user JSON and nonfinite sizing instead of coercing to zeros', () => {
  expect(()=>toBacktestSpec({...form,pool_schedule_text:'{}'})).toThrow()
  expect(()=>toBacktestSpec({...form,sizing:{method:'fixed',proportions:[]},proportions_text:'0.5, nan'})).toThrow()
})

it('saved advanced strategy round-trips without losing rules or allocations', () => {
  const value=toBacktestSpec({...form,codes:'000001',sizing:{method:'factor',field:'MA5',proportions:[]},
    weighting:{method:'rank_ic',window:20},timing:{rules:[{field:'close_ma20',op:'gt',value:1,index:'sse'}],enter_votes:1,exit_votes:1,bear_exposure:.2},timing_enabled:true})
  const restored=toBacktestSpec({...form,...fromStudySpec(value)})
  expect(restored).toEqual(value)
})


it('imports omitted optional sizing proportions without crashing', () => {
  const partial = { ...form, sizing: { method: 'factor', field: 'MA5' } }
  expect(fromStudySpec(partial as Parameters<typeof fromStudySpec>[0]).proportions_text).toBe('')
})

it('clears inherited historical membership snapshots when the editor is emptied', () => {
  const value = { ...form, pool_schedule: [{ as_of: '2026-01-01', stock_codes: ['000001'] }] }
  expect(toBacktestSpec({ ...fromStudySpec(value), ...form, pool_schedule: value.pool_schedule, pool_schedule_text: '' }).pool_schedule).toEqual([])
})
