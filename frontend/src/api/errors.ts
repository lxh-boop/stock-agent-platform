import type { ApiErrorPayload } from '../types/api'
import { isAxiosError } from 'axios'

export class ApiContractError extends Error {
  readonly code: string
  readonly details: Record<string, unknown>
  readonly requestId: string

  constructor(error: ApiErrorPayload, requestId = '') {
    super(error.message || '服务端返回未知错误')
    this.name = 'ApiContractError'
    this.code = error.code || 'UNKNOWN_ERROR'
    this.details = error.details ?? {}
    this.requestId = requestId
  }
}

export function errorMessage(error: unknown): string {
  if (isAxiosError(error)) {
    const body = error.response?.data
    const detail = body?.detail
    const serverMessage = body?.error?.message
    if (typeof serverMessage === 'string' && serverMessage.trim()) return serverMessage
    if (typeof detail === 'string' && detail.trim()) {
      if (detail === 'task_positional_arguments_not_allowed') return '发送请求格式已失效，请刷新页面后重试。'
      return detail
    }
    if (Array.isArray(detail)) {
      // Validation responses also contain the submitted input and context. Only
      // expose field names and validation messages, never the request payload.
      const problems = detail.flatMap((item: unknown) => {
        if (!item || typeof item !== 'object') return []
        const { loc, msg } = item as { loc?: unknown; msg?: unknown }
        if (typeof msg !== 'string') return []
        const field = Array.isArray(loc) ? loc.filter((part) => typeof part === 'string' && part !== 'body').join('.') : ''
        return [field ? `${field}：${msg}` : msg]
      })
      if (problems.length) return `提交内容不符合要求：${problems.slice(0, 3).join('；')}`
    }
    if (error.response?.status === 422) return '提交内容不符合要求，请检查后重试。'
    if (error.response?.status && error.response.status >= 500) return '服务暂时不可用，请稍后重试。'
    if (!error.response) return '网络连接失败，请检查网络后重试。'
  }
  if (error instanceof Error) return error.message
  return String(error ?? '未知错误')
}
