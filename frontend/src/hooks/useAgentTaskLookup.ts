import { useEffect } from 'react'
import { isAxiosError } from 'axios'
import { useAgentTaskStore } from '../stores/agentTaskStore'
import { useAuthStore } from '../stores/authStore'
import { TERMINAL_TASK_STATUSES, type TaskRecord } from '../types/task'

export function isUnavailableAgentTask(error: unknown): boolean {
  return isAxiosError(error) && [403, 404].includes(error.response?.status ?? 0)
}

export function useAgentTaskLookup(taskId: string, conversationId: string, userId: string, data?: TaskRecord, error?: unknown) {
  useEffect(() => {
    const state = useAgentTaskStore.getState()
    const auth = useAuthStore.getState()
    if (!taskId || state.taskId !== taskId || state.conversationId !== conversationId || auth.user?.user_id !== userId) return
    if (isUnavailableAgentTask(error)) {
      // A missing or inaccessible persisted identifier must not lock the
      // composer. Transient failures keep the recoverable task in place.
      state.clear()
      return
    }
    if (!data || data.task_id !== taskId || data.owner_id !== userId || data.session_id !== conversationId) return
    if (state.task && TERMINAL_TASK_STATUSES.has(state.task.status) && !TERMINAL_TASK_STATUSES.has(data.status)) return
    state.setTask(data)
  }, [conversationId, data, error, taskId, userId])
}
