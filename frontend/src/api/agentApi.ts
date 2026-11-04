import { httpClient } from './httpClient'
import { unwrapEnvelope } from './envelope'
import { getWeb } from './webApi'
import type { OperationResponse } from '../types/api'
import type {
  AgentMessage,
  AgentMessagePage,
  AgentPendingActionPage,
  AgentRunDetail,
  AgentSession,
  AgentSessionPage,
  AgentStrategyProposal,
} from '../types/agent'

const BASE = '/api/v1/web/agent'

async function send<T>(
  method: 'post' | 'patch' | 'delete',
  url: string,
  body?: unknown,
  params?: Record<string, unknown>,
): Promise<T> {
  const response = method === 'post'
    ? await httpClient.post<OperationResponse<unknown>>(url, body)
    : method === 'patch'
      ? await httpClient.patch<OperationResponse<unknown>>(url, body)
      : await httpClient.delete<OperationResponse<unknown>>(url, { data: body, params })
  return unwrapEnvelope<T>(response.data)
}

export const agentApi = {
  sessions: (userId: string) =>
    getWeb<AgentSessionPage>(`${BASE}/sessions`, { params: { user_id: userId, limit: 30 } }),
  createSession: (userId: string, language = 'zh') =>
    send<AgentSession>('post', `${BASE}/sessions`, { user_id: userId, language }),
  renameSession: (userId: string, conversationId: string, title: string) =>
    send<AgentSession>('patch', `${BASE}/sessions/${encodeURIComponent(conversationId)}`, { user_id: userId, title }),
  deleteSession: (userId: string, conversationId: string) =>
    send<{ conversation_id: string; deleted: boolean }>(
      'delete',
      `${BASE}/sessions/${encodeURIComponent(conversationId)}`,
      undefined,
      { user_id: userId },
    ),
  messages: (userId: string, conversationId: string, limit = 50) =>
    getWeb<AgentMessagePage>(`${BASE}/sessions/${encodeURIComponent(conversationId)}/messages`, {
      params: { user_id: userId, limit },
    }),
  createMessage: (
    userId: string,
    conversationId: string,
    content: string,
    messageId: string,
    language = 'zh',
  ) =>
    send<AgentMessage>('post', `${BASE}/sessions/${encodeURIComponent(conversationId)}/messages`, {
      user_id: userId,
      role: 'user',
      content,
      language,
      message_id: messageId,
    }),
  finalizeTask: (userId: string, conversationId: string, taskId: string) =>
    send<AgentMessage>('post', `${BASE}/sessions/${encodeURIComponent(conversationId)}/finalize-task`, {
      user_id: userId,
      task_id: taskId,
    }),
  run: (userId: string, runId: string) =>
    getWeb<AgentRunDetail>(`${BASE}/runs/${encodeURIComponent(runId)}`, { params: { user_id: userId } }),
  pendingActions: (userId: string, conversationId: string) =>
    getWeb<AgentPendingActionPage>(`${BASE}/pending-actions`, {
      params: { user_id: userId, conversation_id: conversationId },
    }),
  confirmPendingAction: (
    userId: string,
    conversationId: string,
    planId: string,
    confirmationText: string,
    requestId: string,
    idempotencyKey: string,
  ) =>
    send<Record<string, unknown>>('post', `${BASE}/pending-actions/${encodeURIComponent(planId)}/confirm`, {
      user_id: userId,
      conversation_id: conversationId,
      confirmation_text: confirmationText,
      request_id: requestId,
      idempotency_key: idempotencyKey,
    }),
  rejectPendingAction: (
    userId: string,
    conversationId: string,
    planId: string,
    requestId: string,
    idempotencyKey: string,
  ) =>
    send<Record<string, unknown>>('post', `${BASE}/pending-actions/${encodeURIComponent(planId)}/reject`, {
      user_id: userId,
      conversation_id: conversationId,
      confirmation_text: '',
      request_id: requestId,
      idempotency_key: idempotencyKey,
    }),
  strategyProposal: (userId: string, conversationId: string) =>
    getWeb<AgentStrategyProposal>(
      `${BASE}/sessions/${encodeURIComponent(conversationId)}/strategy-proposal`,
      { params: { user_id: userId } },
    ),
}
