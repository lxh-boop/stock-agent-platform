import type { BacktestSpec, BacktestCondition, AnalysisSpec, StudyKind, PoolSnapshot } from '../types/backtests'

export type BacktestForm = Omit<BacktestSpec, 'stock_codes'> & Partial<AnalysisSpec> & {
  codes?: string; proportions_text?: string; pool_schedule_text?: string; timing_enabled?: boolean
}

export function toBacktestSpec(value: BacktestForm): BacktestSpec {
  const { codes, proportions_text, pool_schedule_text, timing_enabled, segment_count: _a, interval_days: _b, decay_days: _c, ...spec } = value
  const stockCodes = codes?.split(/[\s,，;；]+/).filter(Boolean)
  const conditions = <T extends BacktestCondition>(items?: T[]) => (items ?? []).map(item => {
    const { upper, ...rest } = item
    return { ...rest, ...(item.op === 'between' ? { upper } : {}) } as T
  })
  let pool: PoolSnapshot[] = spec.pool_schedule ?? []
  if (pool_schedule_text !== undefined) {
    try { pool = pool_schedule_text.trim() ? JSON.parse(pool_schedule_text) : [] } catch { throw new Error('历史成员快照必须为合法 JSON 数组') }
    if (!Array.isArray(pool)) throw new Error('历史成员快照必须为数组')
  }
  const method = spec.sizing?.method ?? 'equal'
  const proportions = method === 'fixed' ? (proportions_text === undefined ? (spec.sizing?.proportions ?? []) : proportions_text.split(/[\s,，]+/).filter(Boolean).map(Number)) : []
  if (proportions.some(x => !Number.isFinite(x) || x < 0)) throw new Error('仓位比例必须为非负数字')
  const timing = timing_enabled === false ? null : spec.timing
  return { ...spec, stock_codes: stockCodes?.length ? stockCodes : null, pool_schedule: pool,
    entry_filters: conditions(spec.entry_filters), keep_rules: conditions(spec.keep_rules), sell_filters: conditions(spec.sell_filters),
    market_filters: conditions(spec.market_filters),
    sizing: { method, proportions, field: method === 'factor' ? spec.sizing?.field : null },
    timing: timing ? { ...timing, rules: conditions(timing.rules) } : null }
}

export function toStudySpec(value: BacktestForm, kind: StudyKind): BacktestSpec | AnalysisSpec {
  const spec = toBacktestSpec(value)
  if (kind === 'analysis') return { name: spec.name, start_date: spec.start_date, end_date: spec.end_date,
    factors: spec.factors, stock_codes: spec.stock_codes, pool_schedule: spec.pool_schedule,
    segment_count: value.segment_count ?? 5, interval_days: value.interval_days ?? 5, decay_days: value.decay_days ?? 5 }
  return kind === 'screen' ? { ...spec, start_date: spec.end_date } : spec
}

export function fromStudySpec(spec: BacktestSpec | AnalysisSpec): Partial<BacktestForm> {
  const bt = spec as BacktestSpec
  return { ...spec, codes: spec.stock_codes?.join(', ') ?? '',
    pool_schedule_text: spec.pool_schedule?.length ? JSON.stringify(spec.pool_schedule, null, 2) : '',
    timing_enabled: Boolean(bt.timing), proportions_text: bt.sizing?.proportions?.join(', ') ?? '' }
}

export function backtestError(error: unknown): string {
  const e = error as { response?: { data?: { detail?: unknown } }; message?: string }
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map(row => `${row.loc?.slice(1).join('.') ?? ''} ${row.msg ?? '参数错误'}`.trim()).join('；')
  return e?.message ?? String(error)
}

/** 普通HTTP也支持getRandomValues，不依赖仅安全上下文提供的randomUUID。 */
export function backtestRequestId(): string {
  if (!globalThis.crypto?.getRandomValues) throw new Error('浏览器不支持安全随机数，请更换现代浏览器')
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128
  const h = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`
}
