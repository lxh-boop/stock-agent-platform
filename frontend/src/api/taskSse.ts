import { decodeTransport } from './transport'
import type { TaskEvent, TaskRecord } from '../types/task'

export interface TaskSseCallbacks {
  onEvent: (event: TaskEvent) => void
  onComplete: (task: TaskRecord) => void
  onError?: (message: string) => void
}

export function connectTaskEvents(
  taskId: string,
  after: number,
  callbacks: TaskSseCallbacks,
): () => void {
  const url = `/api/v1/tasks/${encodeURIComponent(taskId)}/events?after=${Math.max(0, after)}`
  const source = new EventSource(url)
  let completed = false
  let disposed = false

  source.addEventListener('task-event', (raw) => {
    if (disposed || completed) return
    const message = raw as MessageEvent<string>
    const event = decodeTransport<TaskEvent>(JSON.parse(message.data))
    if (event.task_id !== taskId) return
    callbacks.onEvent(event)
  })

  source.addEventListener('task-complete', (raw) => {
    if (disposed || completed) return
    const message = raw as MessageEvent<string>
    const payload = decodeTransport<{ task: TaskRecord }>(JSON.parse(message.data))
    if (payload.task.task_id !== taskId) return
    completed = true
    callbacks.onComplete(payload.task)
    source.close()
  })

  source.onerror = () => {
    if (!disposed && !completed && source.readyState === EventSource.CLOSED) {
      callbacks.onError?.('SSE 连接已关闭，可通过 task_id 继续恢复')
    }
  }

  return () => {
    disposed = true
    source.close()
  }
}
