import { useEffect } from 'react'
import { connectTaskEvents } from '../api/taskSse'
import { TERMINAL_TASK_STATUSES } from '../types/task'
import { useTaskStore } from '../stores/taskStore'
import { useAuthStore } from '../stores/authStore'

export function useTaskEvents() {
  const authStatus = useAuthStore((state) => state.status)
  const ownerId = useAuthStore((state) => state.user?.user_id)
  const activeTaskId = useTaskStore((state) => state.activeTaskId)
  const taskStatus = useTaskStore((state) => state.task?.status)
  const appendEvent = useTaskStore((state) => state.appendEvent)
  const setTask = useTaskStore((state) => state.setTask)

  useEffect(() => {
    if (authStatus !== 'authed' || !ownerId || !activeTaskId || (taskStatus && TERMINAL_TASK_STATUSES.has(taskStatus))) return
    const isCurrent = () => {
      const auth = useAuthStore.getState()
      return auth.status === 'authed' && auth.user?.user_id === ownerId && useTaskStore.getState().activeTaskId === activeTaskId
    }
    const after = useTaskStore.getState().lastSequence
    return connectTaskEvents(activeTaskId, after, {
      onEvent: (event) => {
        if (!isCurrent() || event.task_id !== activeTaskId) return
        const current = useTaskStore.getState().task
        if (current && TERMINAL_TASK_STATUSES.has(current.status)) return
        appendEvent(event)
        const progress = Number(event.data.progress)
        const message = typeof event.data.message === 'string' ? event.data.message : undefined
        if (current?.task_id === activeTaskId && (Number.isFinite(progress) || message || event.event_type === 'started')) {
          const status = event.event_type === 'started' ? 'running' : current.status
          setTask({
            ...current,
            status,
            progress: Number.isFinite(progress) ? progress : current.progress,
            message: message ?? current.message,
          })
        }
      },
      onComplete: (task) => {
        if (isCurrent() && task.task_id === activeTaskId) setTask(task)
      },
    })
  }, [activeTaskId, appendEvent, setTask, taskStatus, authStatus, ownerId])
}
