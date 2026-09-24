import { httpClient } from './httpClient'
import { unwrapEnvelope } from './envelope'
import type { OperationResponse } from '../types/api'
import type { SkillCreatePayload, SkillDetail, SkillListData, SkillSummary } from '../types/skills'

export const skillsApi = {
  list: async (): Promise<SkillListData> => {
    const response = await httpClient.get<OperationResponse<unknown>>('/api/v1/web/skills')
    return unwrapEnvelope<SkillListData>(response.data)
  },
  detail: async (name: string): Promise<SkillDetail> => {
    const response = await httpClient.get<OperationResponse<unknown>>(`/api/v1/web/skills/${encodeURIComponent(name)}`)
    return unwrapEnvelope<{ skill: SkillDetail }>(response.data).skill
  },
  create: async (payload: SkillCreatePayload): Promise<SkillSummary> => {
    const response = await httpClient.post<OperationResponse<unknown>>('/api/v1/web/skills', payload)
    return unwrapEnvelope<{ skill: SkillSummary }>(response.data).skill
  },
  upload: async (file: File): Promise<SkillSummary> => {
    const form = new FormData()
    form.append('file', file)
    const response = await httpClient.post<OperationResponse<unknown>>('/api/v1/web/skills/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return unwrapEnvelope<{ skill: SkillSummary }>(response.data).skill
  },
  importGithub: async (url: string): Promise<SkillSummary> => {
    const response = await httpClient.post<OperationResponse<unknown>>('/api/v1/web/skills/import-github', { url })
    return unwrapEnvelope<{ skill: SkillSummary }>(response.data).skill
  },
  enable: async (name: string): Promise<SkillSummary> => {
    const response = await httpClient.post<OperationResponse<unknown>>(`/api/v1/web/skills/${encodeURIComponent(name)}/enable`)
    return unwrapEnvelope<{ skill: SkillSummary }>(response.data).skill
  },
  disable: async (name: string): Promise<SkillSummary> => {
    const response = await httpClient.post<OperationResponse<unknown>>(`/api/v1/web/skills/${encodeURIComponent(name)}/disable`)
    return unwrapEnvelope<{ skill: SkillSummary }>(response.data).skill
  },
  remove: async (name: string): Promise<void> => {
    const response = await httpClient.delete<OperationResponse<unknown>>(`/api/v1/web/skills/${encodeURIComponent(name)}`)
    unwrapEnvelope<unknown>(response.data)
  },
}
