import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AxiosError, type AxiosResponse } from 'axios'
import { useAgentTaskLookup } from '../hooks/useAgentTaskLookup'
import { useAgentTaskEvents } from '../hooks/useAgentTaskEvents'
import { useAgentTaskStore } from '../stores/agentTaskStore'
import { useAuthStore } from '../stores/authStore'
import { connectTaskEvents } from '../api/taskSse'
import type { TaskRecord } from '../types/task'

vi.mock('react', async (original) => ({ ...await original<typeof import('react')>(), useEffect: (effect: () => unknown) => effect() }))
vi.mock('../api/taskSse', () => ({ connectTaskEvents: vi.fn(() => () => {}) }))
vi.mock('../stores/agentTaskStore', async (original) => {
  const actual = await original<typeof import('../stores/agentTaskStore')>()
  const select = (selector: (state: ReturnType<typeof actual.useAgentTaskStore.getState>) => unknown) => selector(actual.useAgentTaskStore.getState())
  return { useAgentTaskStore: Object.assign(select, actual.useAgentTaskStore) }
})
vi.mock('../stores/authStore', async (original) => {
  const actual = await original<typeof import('../stores/authStore')>()
  const select = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { useAuthStore: Object.assign(select, actual.useAuthStore) }
})

function task(id: string, status: TaskRecord['status'] = 'running'): TaskRecord {
  return { task_id: id, owner_id: 'alice', session_id: 'conversation', status, progress: 0.1, message: 'working' } as TaskRecord
}
function httpError(status: number) {
  return new AxiosError('Request failed with status code ' + status, undefined, undefined, undefined, { status, data: { detail: 'task_not_found' } } as AxiosResponse)
}

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  useAuthStore.getState().setUser({ user_id: 'alice', display_name: 'Alice', role: 'user' })
  useAgentTaskStore.getState().clear()
})
afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); useAgentTaskStore.getState().clear() })

describe('Agent task lookup and late events', () => {
  it.each([403, 404])('releases a persisted task that returns HTTP %s', (status) => {
    useAgentTaskStore.getState().setRecovery('missing', 'conversation')
    useAgentTaskLookup('missing', 'conversation', 'alice', undefined, httpError(status))
    expect(useAgentTaskStore.getState().taskId).toBe('')
  })

  it.each([500, 503])('keeps a recoverable task on HTTP %s', (status) => {
    useAgentTaskStore.getState().setTask(task('current', 'failed'))
    useAgentTaskLookup('current', 'conversation', 'alice', undefined, httpError(status))
    expect(useAgentTaskStore.getState().task).toEqual(task('current', 'failed'))
  })

  it('ignores an old lookup or rejection after a newer submission', () => {
    useAgentTaskStore.getState().setTask(task('new'))
    useAgentTaskLookup('old', 'conversation', 'alice', task('old'))
    useAgentTaskLookup('old', 'conversation', 'alice', undefined, httpError(404))
    expect(useAgentTaskStore.getState().task?.task_id).toBe('new')
  })

  it('does not revert a completed task with a delayed running snapshot', () => {
    useAgentTaskStore.getState().setTask(task('current', 'failed'))
    useAgentTaskLookup('current', 'conversation', 'alice', task('current'))
    expect(useAgentTaskStore.getState().task?.status).toBe('failed')
  })

  it('does not restore an inaccessible task from queued SSE callbacks after clearing it', () => {
    useAgentTaskStore.getState().setTask(task('old'))
    useAgentTaskEvents()
    const callbacks = vi.mocked(connectTaskEvents).mock.calls[0][2]
    useAgentTaskLookup('old', 'conversation', 'alice', undefined, httpError(404))
    callbacks.onComplete?.(task('old'))
    expect(useAgentTaskStore.getState().taskId).toBe('')
  })

  it('ignores a late started event after a terminal SSE result', () => {
    useAgentTaskStore.getState().setTask(task('current'))
    useAgentTaskEvents()
    const callbacks = vi.mocked(connectTaskEvents).mock.calls[0][2]
    callbacks.onComplete?.(task('current', 'failed'))
    callbacks.onEvent?.({ task_id: 'current', sequence: 10, event_type: 'started', data: {}, created_at: '' })
    expect(useAgentTaskStore.getState().task?.status).toBe('failed')
  })
})
