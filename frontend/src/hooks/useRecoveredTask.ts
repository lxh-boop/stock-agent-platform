import { useEffect } from 'react'
import { isAxiosError } from 'axios'
import { getTask } from '../api/taskApi'
import { useTaskStore } from '../stores/taskStore'
import { TERMINAL_TASK_STATUSES } from '../types/task'
import { useAuthStore } from '../stores/authStore'

export function useRecoveredTask() {
  const authStatus = useAuthStore((state) => state.status)
  const ownerId = useAuthStore((state) => state.user?.user_id)
  const activeTaskId = useTaskStore((state) => state.activeTaskId)
  const setTask = useTaskStore((state) => state.setTask)

  useEffect(() => {
    if (authStatus !== 'authed' || !ownerId || !activeTaskId) return
    let cancelled = false
    const isCurrent = () => {
      const auth = useAuthStore.getState()
      return !cancelled && auth.status === 'authed' && auth.user?.user_id === ownerId && useTaskStore.getState().activeTaskId === activeTaskId
    }
    getTask(activeTaskId)
      .then((task) => {
        const state = useTaskStore.getState()
        if (!isCurrent() || task.task_id !== activeTaskId) return
        const current = state.task
        if (current?.task_id === activeTaskId) {
          if (TERMINAL_TASK_STATUSES.has(current.status) && !TERMINAL_TASK_STATUSES.has(task.status)) return
          if (Date.parse(task.updated_at) < Date.parse(current.updated_at)) return
        }
        setTask(task)
      })
      .catch((error: unknown) => {
        const status = isAxiosError(error) ? error.response?.status : undefined
        if (isCurrent() && (status === 403 || status === 404)) useTaskStore.getState().clear()
      })
    return () => {
      cancelled = true
    }
  }, [activeTaskId, setTask, authStatus, ownerId])
}
