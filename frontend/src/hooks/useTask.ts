import { useMutation } from '@tanstack/react-query'
import { cancelTask, submitTask } from '../api/taskApi'
import { useCurrentUserId } from './useCurrentUserId'
import { useSessionStore } from '../stores/sessionStore'
import { useTaskStore } from '../stores/taskStore'

export function useDiagnosticTask() {
  const ownerId = useCurrentUserId()
  const sessionId = useSessionStore((state) => state.sessionId)
  const setTask = useTaskStore((state) => state.setTask)
  const clear = useTaskStore((state) => state.clear)

  const submit = useMutation({
    mutationFn: async () => {
      clear()
      return submitTask({
        task_type: 'diagnostic.sleep',
        kwargs: { seconds: 3, steps: 6 },
        owner_id: ownerId,
        session_id: sessionId,
        metadata: { surface: 'react-stage6-1' },
        timeout_seconds: 9930,
        max_retries: 0,
      })
    },
    onSuccess: setTask,
  })

  const cancel = useMutation({
    mutationFn: async (taskId: string) => cancelTask(taskId),
    onSuccess: setTask,
  })

  return { submit, cancel }
}
