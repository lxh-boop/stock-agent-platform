import { useEffect } from 'react'
import { connectTaskEvents } from '../api/taskSse'
import { TERMINAL_TASK_STATUSES } from '../types/task'
import { useAgentTaskStore } from '../stores/agentTaskStore'
import { useAuthStore } from '../stores/authStore'

export function useAgentTaskEvents() {
  const taskId = useAgentTaskStore((state) => state.taskId)
  const status = useAgentTaskStore((state) => state.task?.status)
  const appendEvent = useAgentTaskStore((state) => state.appendEvent)
  const setTask = useAgentTaskStore((state) => state.setTask)
  const ownerId = useAuthStore((state) => state.user?.user_id)

  useEffect(() => {
    if (!ownerId || !taskId || (status && TERMINAL_TASK_STATUSES.has(status))) return
    const isCurrent = () => useAuthStore.getState().user?.user_id === ownerId && useAgentTaskStore.getState().taskId === taskId
    const after = useAgentTaskStore.getState().lastSequence
    return connectTaskEvents(taskId, after, {
      onEvent: (event) => {
        if (!isCurrent() || event.task_id !== taskId) return
        const current = useAgentTaskStore.getState().task
        if (current && TERMINAL_TASK_STATUSES.has(current.status)) return
        appendEvent(event)
        const progress = Number(event.data.progress)
        const message = typeof event.data.message === 'string' ? event.data.message : undefined
        if (current?.task_id === taskId && current.owner_id === ownerId) {
          setTask({
            ...current,
            status: event.event_type === 'started' ? 'running' : current.status,
            progress: Number.isFinite(progress) ? progress : current.progress,
            message: message ?? current.message,
          })
        }
      },
      onComplete: (task) => {
        if (isCurrent() && task.task_id === taskId && task.owner_id === ownerId) setTask(task)
      },
      onError: () => {
        // The task is still recoverable with task_id; polling in AgentPage continues.
      },
    })
  }, [appendEvent, setTask, status, taskId, ownerId])
}
