import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getTask } from '../api/taskApi'
import { connectTaskEvents } from '../api/taskSse'
import { useRecoveredTask } from '../hooks/useRecoveredTask'
import { useTaskEvents } from '../hooks/useTaskEvents'
import { useTaskStore } from '../stores/taskStore'
import { useAuthStore } from '../stores/authStore'
import { authApi } from '../api/authApi'
import type { TaskEvent, TaskRecord, TaskStatus } from '../types/task'

const lifecycle = vi.hoisted(() => ({ cleanups: [] as Array<() => void> }))

// Run the real effect callbacks with cleanup under test control. This models a
// response arriving after selection changes but before React cleans up effects.
vi.mock('react', async (importOriginal) => ({
  ...await importOriginal<typeof import('react')>(),
  useEffect: (effect: () => void | (() => void)) => {
    const cleanup = effect()
    if (cleanup) lifecycle.cleanups.push(cleanup)
  },
}))

vi.mock('../stores/taskStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../stores/taskStore')>()
  const select = (selector: (state: ReturnType<typeof actual.useTaskStore.getState>) => unknown) => selector(actual.useTaskStore.getState())
  return { ...actual, useTaskStore: Object.assign(select, actual.useTaskStore) }
})

vi.mock('../api/taskApi', () => ({ getTask: vi.fn() }))
vi.mock('../api/authApi', () => ({ authApi: { me: vi.fn(), logout: vi.fn() } }))

vi.mock('../stores/authStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../stores/authStore')>()
  const select = (selector: (state: ReturnType<typeof actual.useAuthStore.getState>) => unknown) => selector(actual.useAuthStore.getState())
  return { ...actual, useAuthStore: Object.assign(select, actual.useAuthStore) }
})

class FakeEventSource {
  static CLOSED = 2
  static instances: FakeEventSource[] = []
  readyState = 1
  listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>()
  onerror: (() => void) | null = null
  constructor(readonly url: string) { FakeEventSource.instances.push(this) }
  addEventListener(name: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(name, [...this.listeners.get(name) ?? [], listener])
  }
  close() { this.readyState = FakeEventSource.CLOSED }
  emit(name: string, data: unknown) {
    // Deliberately allow delivery after close to exercise queued callbacks.
    for (const listener of this.listeners.get(name) ?? []) listener({ data: JSON.stringify(data) } as MessageEvent<string>)
  }
}

function task(id: string, status: TaskStatus = 'running'): TaskRecord {
  return {
    task_id: id, task_type: 'personal-daily.run', owner_id: 'owner', session_id: 'session',
    status, progress: 0.2, message: `${id} ${status}`,
    created_at: '2026-11-18T03:00:00Z', updated_at: '2026-11-18T03:00:01Z',
    timeout_seconds: 100, max_retries: 0, attempt: 1, cancel_requested: false,
  }
}

function event(id: string, sequence = 500): TaskEvent {
  return { task_id: id, sequence, event_type: 'progress', data: { message: `${id} event`, progress: 0.7 }, created_at: '2026-11-18T03:00:02Z' }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function settle() { await Promise.resolve(); await Promise.resolve() }

beforeEach(() => {
  vi.stubGlobal('EventSource', FakeEventSource)
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  FakeEventSource.instances = []
  useTaskStore.getState().clear()
  useAuthStore.getState().setUser({ user_id: 'owner', display_name: 'Owner', role: 'user' })
})

afterEach(() => {
  for (const cleanup of lifecycle.cleanups.splice(0)) cleanup()
  useTaskStore.getState().clear()
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('task selection and recovery isolation', () => {
  it('starts a new event history when selecting another task, while retaining same-task progress', () => {
    useTaskStore.getState().setTask(task('old'))
    useTaskStore.getState().appendEvent(event('old'))
    useTaskStore.getState().setTask({ ...task('old'), progress: 0.8 })
    expect(useTaskStore.getState().lastSequence).toBe(500)
    useTaskStore.getState().setTask(task('new'))
    expect(useTaskStore.getState().events).toEqual([])
    expect(useTaskStore.getState().lastSequence).toBe(0)
    useTaskStore.getState().appendEvent(event('old', 501))
    expect(useTaskStore.getState().events).toEqual([])
  })

  it.each(['pending-submit', 'new-task'] as const)('ignores old SSE events/completion during %s before cleanup', (phase) => {
    useTaskStore.getState().setTask(task('old'))
    useTaskEvents()
    const oldSource = FakeEventSource.instances[0]
    useTaskStore.getState().clear()
    if (phase === 'new-task') useTaskStore.getState().setTask(task('new', 'queued'))
    oldSource.emit('task-event', event('old'))
    oldSource.emit('task-complete', { task: task('old', 'failed') })
    expect(useTaskStore.getState().activeTaskId).toBe(phase === 'new-task' ? 'new' : '')
    expect(useTaskStore.getState().events).toEqual([])
  })

  it('retains the current task failure and its error payload', () => {
    useTaskStore.getState().setTask(task('new'))
    useTaskEvents()
    const failed = { ...task('new', 'failed'), error: { message: 'new run failed' } }
    FakeEventSource.instances[0].emit('task-complete', { task: failed })
    expect(useTaskStore.getState().task).toEqual(failed)
  })

  it('does not let a late started event regress a terminal task before cleanup', () => {
    useTaskStore.getState().setTask(task('new'))
    useTaskEvents()
    const failed = task('new', 'failed')
    useTaskStore.getState().setTask(failed)
    FakeEventSource.instances[0].emit('task-event', { ...event('new'), event_type: 'started' })
    expect(useTaskStore.getState().task).toEqual(failed)
  })

  it.each(['pending-submit', 'new-task'] as const)('ignores an old recovery response during %s before cleanup', async (phase) => {
    const response = deferred<TaskRecord>()
    vi.mocked(getTask).mockReturnValueOnce(response.promise)
    useTaskStore.getState().setTask(task('old'))
    useRecoveredTask()
    useTaskStore.getState().clear()
    if (phase === 'new-task') useTaskStore.getState().setTask(task('new', 'failed'))
    response.resolve(task('old', 'failed'))
    await settle()
    expect(useTaskStore.getState().activeTaskId).toBe(phase === 'new-task' ? 'new' : '')
  })

  it('does not clear the new failed task when an old recovery request fails', async () => {
    const response = deferred<TaskRecord>()
    vi.mocked(getTask).mockReturnValueOnce(response.promise)
    useTaskStore.getState().setTask(task('old'))
    useRecoveredTask()
    const failed = task('new', 'failed')
    useTaskStore.getState().setTask(failed)
    response.reject(new Error('old task unavailable'))
    await settle()
    expect(useTaskStore.getState().task).toEqual(failed)
  })

  it.each(['network', 'server'] as const)('preserves a real SSE failure when the same-task recovery GET later fails with %s error', async (kind) => {
    const response = deferred<TaskRecord>()
    vi.mocked(getTask).mockReturnValueOnce(response.promise)
    useTaskStore.getState().setTask(task('new'))
    useRecoveredTask()
    useTaskEvents()
    const failed = { ...task('new', 'failed'), message: 'today run failed', error: { message: 'today failure details' } }
    FakeEventSource.instances[0].emit('task-complete', { task: failed })
    response.reject(kind === 'network' ? new Error('network unavailable') : Object.assign(new Error('server unavailable'), { isAxiosError: true, response: { status: 500 } }))
    await settle()
    expect(useTaskStore.getState().task).toEqual(failed)
  })

  it.each([403, 404])('clears the selected task when recovery explicitly returns HTTP %s', async (status) => {
    vi.mocked(getTask).mockRejectedValueOnce(Object.assign(new Error('task inaccessible'), { isAxiosError: true, response: { status } }))
    useTaskStore.getState().setTask(task('old'))
    useRecoveredTask()
    await settle()
    expect(useTaskStore.getState().activeTaskId).toBe('')
  })

  it('does not regress a terminal task when its earlier recovery GET finishes late', async () => {
    const response = deferred<TaskRecord>()
    vi.mocked(getTask).mockReturnValueOnce(response.promise)
    useTaskStore.getState().setTask(task('new', 'queued'))
    useRecoveredTask()
    const failed = { ...task('new', 'failed'), updated_at: '2026-11-18T03:00:04Z' }
    useTaskStore.getState().setTask(failed)
    response.resolve(task('new', 'queued'))
    await settle()
    expect(useTaskStore.getState().task).toEqual(failed)
  })

  it('can still explicitly select and recover a historical failure', async () => {
    const failed = task('historical', 'failed')
    vi.mocked(getTask).mockResolvedValueOnce(failed)
    useTaskStore.getState().setTask(task('historical', 'queued'))
    useRecoveredTask()
    await settle()
    expect(useTaskStore.getState().task).toEqual(failed)
  })
})

describe('task authentication boundary', () => {
  it.each(['unknown', 'anonymous'] as const)('does not recover or subscribe while authentication is %s', (status) => {
    useAuthStore.setState({ user: null, status })
    useTaskStore.getState().setTask(task('saved'))
    useRecoveredTask()
    useTaskEvents()
    expect(getTask).not.toHaveBeenCalled()
    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('preserves and recovers the saved task after same-account authentication', async () => {
    useTaskStore.getState().setTask(task('saved'))
    useTaskStore.setState({ task: null })
    useAuthStore.setState({ user: null, status: 'unknown' })
    useAuthStore.getState().setUser({ user_id: 'owner', display_name: 'Owner', role: 'user' })
    vi.mocked(getTask).mockResolvedValueOnce(task('saved', 'failed'))
    useRecoveredTask()
    await settle()
    expect(getTask).toHaveBeenCalledWith('saved')
    expect(useTaskStore.getState().task?.task_id).toBe('saved')
  })

  it('clears task selection on account change and ignores old-account callbacks', async () => {
    const response = deferred<TaskRecord>()
    vi.mocked(getTask).mockReturnValueOnce(response.promise)
    useTaskStore.getState().setTask(task('old'))
    useRecoveredTask()
    useTaskEvents()
    const source = FakeEventSource.instances[0]
    useAuthStore.getState().setUser({ user_id: 'other-owner', display_name: 'Other', role: 'user' })
    expect(useTaskStore.getState().activeTaskId).toBe('')
    response.resolve(task('old', 'failed'))
    source.emit('task-complete', { task: task('old', 'failed') })
    await settle()
    expect(useTaskStore.getState().task).toBeNull()
  })

  it('clears immediately on logout and does not undo a later login when logout returns', async () => {
    const response = deferred<void>()
    vi.mocked(authApi.logout).mockReturnValueOnce(response.promise)
    useTaskStore.getState().setTask(task('old'))
    useTaskEvents()
    const source = FakeEventSource.instances[0]
    const logout = useAuthStore.getState().logout()
    expect(useAuthStore.getState().status).toBe('anonymous')
    expect(useTaskStore.getState().activeTaskId).toBe('')
    source.emit('task-complete', { task: task('old', 'failed') })
    expect(useTaskStore.getState().task).toBeNull()
    useAuthStore.getState().setUser({ user_id: 'other-owner', display_name: 'Other', role: 'user' })
    response.resolve()
    await logout
    expect(useAuthStore.getState().user?.user_id).toBe('other-owner')
  })
})

describe('SSE subscription lifecycle', () => {
  it('ignores messages from another task and callbacks delivered after disposal', () => {
    const callbacks = { onEvent: vi.fn(), onComplete: vi.fn(), onError: vi.fn() }
    const close = connectTaskEvents('new', 0, callbacks)
    const source = FakeEventSource.instances[0]
    source.emit('task-event', event('old'))
    source.emit('task-complete', { task: task('old', 'failed') })
    expect(callbacks.onEvent).not.toHaveBeenCalled()
    expect(callbacks.onComplete).not.toHaveBeenCalled()
    source.emit('task-event', event('new'))
    expect(callbacks.onEvent).toHaveBeenCalledTimes(1)
    close()
    source.emit('task-event', event('new', 501))
    source.emit('task-complete', { task: task('new', 'failed') })
    source.onerror?.()
    expect(callbacks.onEvent).toHaveBeenCalledTimes(1)
    expect(callbacks.onComplete).not.toHaveBeenCalled()
    expect(callbacks.onError).not.toHaveBeenCalled()
  })
})
