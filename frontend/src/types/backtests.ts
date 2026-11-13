export type StudyKind = 'backtest' | 'analysis' | 'screen'
export interface FactorChoice { field: string; direction: 1 | -1; weight: number }
export interface BacktestCondition { field: string; op: 'gt' | 'lt' | 'ge' | 'le' | 'eq' | 'between'; value: number; upper?: number }
export interface MarketCondition extends BacktestCondition { index: 'hs300' | 'csi500' | 'sse' }
export interface PoolSnapshot { as_of: string; stock_codes: string[] }
export interface DataSpec {
  name: string; start_date: string; end_date: string; factors: FactorChoice[]
  stock_codes?: string[] | null; pool_schedule?: PoolSnapshot[]
}
export interface BacktestSpec extends DataSpec {
  entry_filters?: BacktestCondition[]; keep_rules?: BacktestCondition[]; sell_filters?: BacktestCondition[]
  market_filters?: MarketCondition[]
  weighting?: { method: 'fixed' | 'rank_ic'; window: number }
  sizing?: { method: 'equal' | 'fixed' | 'factor'; proportions: number[]; field?: string | null }
  timing?: { rules: MarketCondition[]; enter_votes: number; exit_votes: number; bear_exposure: number } | null
  benchmark?: 'pool' | 'hs300' | 'csi500' | 'sse'
  initial_cash: number; max_positions: number; rebalance_days: number; exposure: number
  min_position?: number; position_band?: number; keep_invested?: boolean; limit_pct?: number | null
  execution: 'open' | 'close'; buy_fee: number; sell_fee: number; slippage_bps: number
  stop_loss?: number | null; take_profit?: number | null; trailing_stop?: number | null; max_hold_days?: number | null
  rank_out?: number | null; exclude_ranges?: { start: string; end: string }[]
}
export interface AnalysisSpec extends DataSpec { segment_count: number; interval_days: number; decay_days: number }
export interface BacktestCatalog {
  fields: string[]; message: string; max_stocks: number; max_positions: number; indexes: Record<string, string>; index_fields: string[]
  schemas: Record<StudyKind, Record<string, unknown>>
  coverage: { start_date: string; end_date: string; stock_count: number; feature_version: string } | null
}
export interface BacktestSummary {
  run_id: string; kind: StudyKind; created_at: string; spec: BacktestSpec | AnalysisSpec
  metrics: Record<string, number | null>; engine_version: string
}
export interface BacktestDetail extends BacktestSummary {
  data_hash: string; data_snapshot: Record<string, unknown>; diagnostics: Record<string, number>
  assumptions: string[]; disclaimer: string; factor_metrics?: Record<string, Record<string, unknown>>
}
export interface SavedStrategy { strategy_id: string; updated_at: string; spec: BacktestSpec }
export type BacktestRowKind = 'equity' | 'trades' | 'positions' | 'skipped_orders' | 'signals' | 'periods' | 'ic_series' | 'segments' | 'long_short' | 'ic_decay' | 'correlation' | 'selections'
export interface BacktestRows { records: Record<string, unknown>[]; total: number; offset: number; limit: number }
