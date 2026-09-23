import { httpClient } from './httpClient'
import { unwrapEnvelope } from './envelope'
import { getWeb } from './webApi'
import type { OperationResponse } from '../types/api'
import type {
  BackfillPreviewRequest,
  CashFlowPreviewRequest,
  PaperProposalList,
  PaperTradingDayHistory,
  PaperTradingSnapshot,
  ProfileUpdateRequest,
  ProposalCommitRequest,
  ProposalRejectRequest,
} from '../types/paperTrading'

const BASE = '/api/v1/web/paper-trading'

async function send<T>(method: 'post' | 'put', url: string, body: unknown): Promise<T> {
  const response = method === 'post'
    ? await httpClient.post<OperationResponse<unknown>>(url, body)
    : await httpClient.put<OperationResponse<unknown>>(url, body)
  return unwrapEnvelope<T>(response.data)
}

export const paperTradingApi = {
  summary: (userId: string) => getWeb<PaperTradingSnapshot>(`${BASE}/summary`, { params: { user_id: userId } }),
  history: (userId: string, tradeDate: string) => getWeb<PaperTradingDayHistory>(`${BASE}/history`, { params: { user_id: userId, trade_date: tradeDate } }),
  updateProfile: (request: ProfileUpdateRequest) => send<Record<string, unknown>>('put', `${BASE}/profile`, request),
  proposals: (userId: string) => getWeb<PaperProposalList>(`${BASE}/proposals`, { params: { user_id: userId } }),
  previewBackfill: (request: BackfillPreviewRequest) => send<Record<string, unknown>>('post', `${BASE}/backfill/preview`, request),
  previewCashFlow: (request: CashFlowPreviewRequest) => send<Record<string, unknown>>('post', `${BASE}/cash-flows/preview`, request),
  commitProposal: (planId: string, request: ProposalCommitRequest) => send<Record<string, unknown>>('post', `${BASE}/proposals/${encodeURIComponent(planId)}/commit`, request),
  rejectProposal: (planId: string, request: ProposalRejectRequest) => send<Record<string, unknown>>('post', `${BASE}/proposals/${encodeURIComponent(planId)}/reject`, request),
}
