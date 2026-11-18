import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { QueryClient } from '@tanstack/react-query'
import { useResearchTaskEvents } from '../hooks/useResearchTaskEvents'
import { connectTaskEvents } from '../api/taskSse'
import { useAuthStore } from '../stores/authStore'
import type { TaskRecord } from '../types/task'

const mocked = vi.hoisted(() => ({ client: null as QueryClient | null, cleanup: null as (() => void) | null }))
vi.mock('react', async (original) => ({ ...await original<typeof import('react')>(),
  useEffect: (effect: () => (() => void) | undefined) => { mocked.cleanup = effect() ?? null },
}))
vi.mock('@tanstack/react-query', async (original) => ({ ...await original<typeof import('@tanstack/react-query')>(),
  useQueryClient: () => mocked.client,
}))
vi.mock('../api/taskSse', () => ({ connectTaskEvents: vi.fn(() => vi.fn()) }))

const key = ['agent-research', 'alice', 'conversation']
const task = (status: TaskRecord['status'] = 'running') => ({task_id:'research-1', owner_id:'alice', session_id:'conversation',
  task_type:'backtest.run', status, progress:.1, message:'working'} as TaskRecord)

beforeEach(() => {
  mocked.client = new QueryClient()
  mocked.client.setQueryData(key, [task()])
  useAuthStore.getState().setUser({user_id:'alice',display_name:'Alice',role:'user'})
})
afterEach(() => { mocked.cleanup?.(); mocked.client?.clear(); vi.clearAllMocks() })

it('updates research progress and completion without sharing the chat task slot', () => {
  useResearchTaskEvents('alice','conversation','research-1')
  const callbacks = vi.mocked(connectTaskEvents).mock.calls[0][2]
  callbacks.onEvent({task_id:'research-1', sequence:1, event_type:'progress', data:{progress:.5, message:'computed'},created_at:''})
  expect(mocked.client?.getQueryData<TaskRecord[]>(key)?.[0].progress).toBe(.5)
  callbacks.onComplete({...task('succeeded'),result:{run_id:'report'}})
  callbacks.onEvent({task_id:'research-1', sequence:2, event_type:'started', data:{progress:.01},created_at:''})
  expect(mocked.client?.getQueryData<TaskRecord[]>(key)?.[0].status).toBe('succeeded')
})

it('ignores cross-owner completions and late events after unmount or sign out', () => {
  useResearchTaskEvents('alice','conversation','research-1')
  const callbacks = vi.mocked(connectTaskEvents).mock.calls[0][2]
  callbacks.onComplete({...task('succeeded'),owner_id:'bob'})
  expect(mocked.client?.getQueryData<TaskRecord[]>(key)?.[0].status).toBe('running')
  mocked.cleanup?.()
  callbacks.onComplete(task('succeeded'))
  expect(mocked.client?.getQueryData<TaskRecord[]>(key)?.[0].status).toBe('running')
  useResearchTaskEvents('alice','conversation','research-1')
  useAuthStore.getState().setUser({user_id:'bob',display_name:'Bob',role:'user'})
  vi.mocked(connectTaskEvents).mock.calls[1][2].onComplete(task('succeeded'))
  expect(mocked.client?.getQueryData<TaskRecord[]>(key)?.[0].status).toBe('running')
})
