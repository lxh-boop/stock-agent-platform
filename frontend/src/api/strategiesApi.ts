import { httpClient } from './httpClient'
import { unwrapEnvelope } from './envelope'
import { getWeb } from './webApi'
import type { BacktestSpec } from '../types/backtests'
import type { TaskRecord } from '../types/task'
import type { StrategyWorkspaceItem, StrategyWorkspaceList } from '../types/strategies'

const root = '/api/v1/web/strategies'
export const strategiesApi = {
  list: (offset = 0, limit = 12) => getWeb<StrategyWorkspaceList>(root, { params: { offset, limit } }),
  detail: (id: string) => getWeb<StrategyWorkspaceItem>(`${root}/${encodeURIComponent(id)}`),
  fromReport: async (runId: string, name: string | undefined, requestId: string) =>
    unwrapEnvelope<StrategyWorkspaceItem>((await httpClient.post(`${root}/from-report`, {
      run_id: runId, name, request_id: requestId, confirmed: true,
    })).data),
  save: async (id: string, spec: BacktestSpec, requestId: string, expectedVersion?: number) =>
    unwrapEnvelope<StrategyWorkspaceItem>((await httpClient.put(`${root}/${encodeURIComponent(id)}`, {
      spec, request_id: requestId, expected_version: expectedVersion, confirmed: true,
    })).data),
  rerun: async (id: string, requestId: string, expectedVersion: number) =>
    unwrapEnvelope<TaskRecord>((await httpClient.post(`${root}/${encodeURIComponent(id)}/rerun`, {
      request_id: requestId, expected_version: expectedVersion, confirmed: true,
    })).data),
  remove: async (id: string, requestId: string, expectedVersion: number) =>
    unwrapEnvelope<{ deleted: boolean }>((await httpClient.delete(`${root}/${encodeURIComponent(id)}`, {
      data: { request_id: requestId, expected_version: expectedVersion, confirmed: true },
    })).data),
}
