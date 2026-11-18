import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { connectTaskEvents } from '../api/taskSse'
import { useAuthStore } from '../stores/authStore'
import { TERMINAL_TASK_STATUSES, type TaskRecord } from '../types/task'

/** Research tasks are separate from the Agent chat task and its recovery slot. */
export function useResearchTaskEvents(userId: string, conversationId: string, activeIds: string) {
  const client = useQueryClient()
  useEffect(() => {
    if (!userId || !conversationId || !activeIds) return
    let disposed = false
    const key = ['agent-research', userId, conversationId]
    const current = () => !disposed && useAuthStore.getState().user?.user_id === userId
    const cleanups = activeIds.split(',').map(taskId => connectTaskEvents(taskId, 0, {
      onEvent: event => {
        if (!current() || event.task_id !== taskId) return
        client.setQueryData<TaskRecord[]>(key, records => records?.map(task => {
          if (task.task_id !== taskId || task.owner_id !== userId || task.session_id !== conversationId || TERMINAL_TASK_STATUSES.has(task.status)) return task
          const progress = event.data.progress
          return { ...task, status: event.event_type === 'started' ? 'running' : task.status,
            progress: typeof progress === 'number' && Number.isFinite(progress) ? progress : task.progress,
            message: typeof event.data.message === 'string' ? event.data.message : task.message }
        }))
      },
      onComplete: completed => {
        if (!current() || completed.task_id !== taskId || completed.owner_id !== userId || completed.session_id !== conversationId) return
        client.setQueryData<TaskRecord[]>(key, records => records?.map(task => task.task_id === taskId ? completed : task))
      },
      // The list query continues polling if the connection is unavailable.
      onError: () => {},
    }))
    return () => { disposed = true; cleanups.forEach(close => close()) }
  }, [activeIds, client, conversationId, userId])
}
