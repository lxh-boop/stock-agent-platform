import { getWeb } from './webApi'
import { httpClient } from './httpClient'
import { unwrapEnvelope } from './envelope'
import type { FactorCatalog, FactorCreateRequest, FactorDefinition, FactorDraft, FactorValidation, FactorValues } from '../types/factors'

const root = '/api/v1/web/factors'

export const factorApi = {
  catalog: () => getWeb<FactorCatalog>(root),
  values: (id: string, code = '') => getWeb<FactorValues>(`${root}/${encodeURIComponent(id)}/values`, {
    params: { limit: 30, ...(code ? { code } : {}) },
  }),
  validate: async (draft: FactorDraft) => unwrapEnvelope<FactorValidation>((await httpClient.post(`${root}/validate`, draft)).data),
  create: async (request: FactorCreateRequest) => unwrapEnvelope<{ factor: FactorDefinition; replayed: boolean }>((await httpClient.post(root, request)).data),
}
