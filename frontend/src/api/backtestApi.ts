import { getWeb } from './webApi'
import { httpClient } from './httpClient'
import { unwrapEnvelope } from './envelope'
import type { BacktestCatalog, BacktestDetail, BacktestRows, BacktestRowKind, BacktestSpec, BacktestSummary, AnalysisSpec, StudyKind, SavedStrategy } from '../types/backtests'
import type { TaskRecord } from '../types/task'

const root = '/api/v1/web/backtests'
export const backtestApi = {
  catalog: () => getWeb<BacktestCatalog>(`${root}/catalog`),
  history: (offset = 0) => getWeb<{ records: BacktestSummary[]; total: number }>(root, { params: { offset, limit: 30 } }),
  detail: (id: string) => getWeb<BacktestDetail>(`${root}/${encodeURIComponent(id)}`),
  rows: (id: string, kind: BacktestRowKind, offset = 0, limit = 50, factor?: string) => getWeb<BacktestRows>(`${root}/${encodeURIComponent(id)}/rows`, { params: { kind, offset, limit, factor } }),
  run: async (spec: BacktestSpec | AnalysisSpec, requestId: string, kind: StudyKind = 'backtest') => {
    const path = kind === 'analysis' ? '/analyses' : kind === 'screen' ? '/screen' : ''
    const response = await httpClient.post(root + path, { spec, request_id: requestId, confirmed: true })
    return unwrapEnvelope<TaskRecord>(response.data)
  },
  strategies: (offset = 0) => getWeb<{ records: SavedStrategy[]; total: number }>(`${root}/strategies`, { params: { offset, limit: 30 } }),
  saveStrategy: async (strategy_id: string, spec: BacktestSpec) => unwrapEnvelope<SavedStrategy>((await httpClient.put(`${root}/strategies`, { strategy_id, spec, confirmed: true })).data),
  deleteStrategy: async (id: string) => httpClient.delete(`${root}/strategies/${encodeURIComponent(id)}`, { data: { confirmed: true } }),
  exportReport: async (id: string) => (await httpClient.get<Blob>(`${root}/${encodeURIComponent(id)}/export`, { responseType: 'blob' })).data,
  deleteReport: async (id: string) => httpClient.delete(`${root}/${encodeURIComponent(id)}`, { data: { confirmed: true } }),
}
