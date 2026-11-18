import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MutationObserver, QueryClient, QueryObserver, type MutationObserverOptions } from '@tanstack/react-query'
import { message } from 'antd'
import { SchedulerStatusPanel } from '../components/dashboard/SchedulerStatusPanel'
import { submitTask } from '../api/taskApi'
import type { PublicSettings } from '../types/settings'
import type { TaskRecord } from '../types/task'

const runtime = vi.hoisted(() => ({
  client: undefined as QueryClient | undefined,
  mutation: undefined as MutationObserver<TaskRecord, Error, void> | undefined,
  setTask: vi.fn(),
  clear: vi.fn(),
  auth: { status: 'authed', user: { user_id: 'owner-a' } } as { status: string; user: { user_id: string } | null },
}))

vi.mock('react', async (original) => ({ ...await original<typeof import('react')>(), useEffect: vi.fn() }))
vi.mock('@tanstack/react-query', async (original) => {
  const actual = await original<typeof import('@tanstack/react-query')>()
  return {
    ...actual,
    useQueryClient: () => runtime.client,
    useMutation: (options: MutationObserverOptions<TaskRecord, Error, void>) => {
      runtime.mutation = new actual.MutationObserver(runtime.client!, options)
      return { mutate: runtime.mutation.mutate.bind(runtime.mutation), isPending: false }
    },
  }
})
vi.mock('../api/taskApi', () => ({ submitTask: vi.fn() }))
vi.mock('../hooks/useCurrentUserId', () => ({ useCurrentUserId: () => 'owner-a' }))
vi.mock('../stores/authStore', () => ({ useAuthStore: { getState: () => runtime.auth } }))
vi.mock('../stores/sessionStore', () => ({
  useSessionStore: (select: (state: { sessionId: string }) => unknown) => select({ sessionId: 'session-a' }),
}))
vi.mock('../stores/taskStore', () => ({
  useTaskStore: (select: (state: { task: null; setTask: typeof runtime.setTask; clear: typeof runtime.clear }) => unknown) =>
    select({ task: null, setTask: runtime.setTask, clear: runtime.clear }),
}))
vi.mock('antd', () => ({
  Alert: 'div', Button: 'button', Card: 'div', Descriptions: Object.assign('div', { Item: 'div' }),
  Modal: { confirm: vi.fn() }, Space: 'div', Tag: 'span', Typography: { Text: 'span', Paragraph: 'p' },
  message: { success: vi.fn(), error: vi.fn() },
}))

function settings(error: string, status: string): PublicSettings {
  return { scheduler: { hour: 18, minute: 0, last_error: error, last_status: status } } as PublicSettings
}

function task(status: TaskRecord['status']): TaskRecord {
  return { task_id: 'new-daily-task', task_type: 'paper-profile.scheduler-manual', owner_id: 'owner-a', status } as TaskRecord
}

describe('daily task submission status', () => {
  let unsubscribe: (() => void) | undefined

  beforeEach(() => {
    vi.clearAllMocks()
    runtime.auth = { status: 'authed', user: { user_id: 'owner-a' } }
    runtime.client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  })

  afterEach(() => {
    unsubscribe?.()
    runtime.client?.clear()
  })

  it.each([
    ['queued', '', 'pending'],
    ['failed', 'The newly submitted task failed', 'failed'],
  ] as const)('refreshes previous scheduler failure when the accepted task is %s', async (taskStatus, currentError, currentStatus) => {
    const previous = settings('Previous task failed', 'failed')
    const current = settings(currentError, currentStatus)
    const queryKey = ['web', 'settings', 'owner-a']
    runtime.client!.setQueryData(queryKey, previous)
    const query = new QueryObserver(runtime.client!, { queryKey, queryFn: async () => current, staleTime: Infinity })
    unsubscribe = query.subscribe(() => undefined)
    vi.mocked(submitTask).mockResolvedValue(task(taskStatus))

    SchedulerStatusPanel({ settings: previous })
    await runtime.mutation!.mutate()

    expect(runtime.client!.getQueryData<PublicSettings>(queryKey)?.scheduler).toEqual(current.scheduler)
    expect(runtime.setTask).toHaveBeenCalledWith(task(taskStatus))
    expect(previous.scheduler.last_error).toBe('Previous task failed')
  })

  it.each(['logout', 'other-account'] as const)('ignores an accepted task arriving after %s', async (change) => {
    let accept!: (record: TaskRecord) => void
    vi.mocked(submitTask).mockReturnValue(new Promise((resolve) => { accept = resolve }))
    SchedulerStatusPanel({ settings: settings('Previous task failed', 'failed') })
    const submitted = runtime.mutation!.mutate()
    await vi.waitFor(() => expect(submitTask).toHaveBeenCalledOnce())

    runtime.auth = change === 'logout'
      ? { status: 'anonymous', user: null }
      : { status: 'authed', user: { user_id: 'owner-b' } }
    accept(task('queued'))
    await submitted

    expect(runtime.setTask).not.toHaveBeenCalled()
  })

  it('rejects a task response owned by another account', async () => {
    vi.mocked(submitTask).mockResolvedValue({ ...task('queued'), owner_id: 'owner-b' })
    SchedulerStatusPanel({ settings: settings('', 'unknown') })
    await runtime.mutation!.mutate()
    expect(runtime.setTask).not.toHaveBeenCalled()
  })

  it.each(['same-account', 'logout', 'other-account'] as const)('shows a delayed submission error only for %s', async (change) => {
    let fail!: (error: Error) => void
    vi.mocked(submitTask).mockReturnValue(new Promise((_resolve, reject) => { fail = reject }))
    SchedulerStatusPanel({ settings: settings('', 'unknown') })
    const submitted = runtime.mutation!.mutate()
    const rejected = expect(submitted).rejects.toThrow('Submission failed')
    await vi.waitFor(() => expect(submitTask).toHaveBeenCalledOnce())

    if (change === 'logout') runtime.auth = { status: 'anonymous', user: null }
    if (change === 'other-account') runtime.auth = { status: 'authed', user: { user_id: 'owner-b' } }
    fail(new Error('Submission failed'))
    await rejected

    if (change === 'same-account') expect(message.error).toHaveBeenCalledWith(expect.stringContaining('Submission failed'))
    else expect(message.error).not.toHaveBeenCalled()
  })
})
