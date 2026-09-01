import type { TablePayload } from './common'

export type GenericRecord = Record<string, unknown>

export interface PaperTradingSnapshot {
  user_id: string
  is_available: boolean
  account: GenericRecord
  positions: TablePayload<GenericRecord>
  orders: TablePayload<GenericRecord>
  nav_history: TablePayload<GenericRecord>
  decisions: TablePayload<GenericRecord>
  risk_report: GenericRecord
  execution_diagnostics: GenericRecord
  trading_settings: GenericRecord
  order_snapshot_dates: unknown[]
  position_snapshot_dates: unknown[]
  profile: GenericRecord
  profile_complete: boolean
  profile_options: Record<string, string[]>
  cash_flows: TablePayload<GenericRecord>
  backfill_status: GenericRecord
  ai_reliability: GenericRecord
  scheduler: GenericRecord
  model_context: GenericRecord
}

export interface PaperTradingHistorySummary {
  position_count: number
  operation_count: number
  buy_count: number
  sell_count: number
  ohlc_matched_count: number
  ohlc_missing_count: number
  close_matched_count: number
}

export interface PaperTradingDayHistory {
  user_id: string
  trade_date: string
  available_dates: string[]
  has_position_snapshot: boolean
  positions: TablePayload<GenericRecord>
  operations: TablePayload<GenericRecord>
  buy_operations: TablePayload<GenericRecord>
  sell_operations: TablePayload<GenericRecord>
  summary: PaperTradingHistorySummary
}

export interface PaperProfilePayload {
  user_id: string
  profile: GenericRecord
  complete: boolean
  options: Record<string, string[]>
}

export interface PaperProposal {
  plan_id: string
  intent: string
  operation_type: string
  confirmation_status: string
  execution_status: string
  expires_at?: string | null
  created_at?: string | null
  before_state_summary: GenericRecord
  proposed_changes: unknown[]
  after_state_preview: GenericRecord
  warnings: unknown[]
  validation_results: GenericRecord
  confirmation_phrase: string
  token_present: boolean
}

export interface PaperProposalList {
  records: PaperProposal[]
  total: number
}

export interface WriteRequestMeta {
  request_id: string
  idempotency_key: string
}

export interface ProfileUpdateRequest extends WriteRequestMeta {
  user_id: string
  profile: GenericRecord
  confirmed: boolean
}

export interface CashFlowPreviewRequest extends WriteRequestMeta {
  user_id: string
  flow_type: 'deposit' | 'withdrawal'
  amount: number
  effective_date?: string
  reason?: string
}

export interface BackfillPreviewRequest extends WriteRequestMeta {
  user_id: string
  start_date: string
  end_date: string
  initial_cash?: number
  force: boolean
  resume: boolean
}

export interface ProposalCommitRequest extends WriteRequestMeta {
  user_id: string
  confirmation_text: string
}

export interface ProposalRejectRequest extends WriteRequestMeta {
  user_id: string
  reason?: string
}

export interface CashFlowCancelRequest extends WriteRequestMeta {
  user_id: string
  confirmed: boolean
}
