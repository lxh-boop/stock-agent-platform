import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryObserver, type QueryObserverOptions } from '@tanstack/react-query'
import { SystemMonitorPage } from '../pages/monitor/SystemMonitorPage'
import { monitorApi } from '../api/monitorApi'

const runtime = vi.hoisted(() => ({
  client: undefined as QueryClient | undefined,
  owner: 'alice' as string | undefined,
  observers: [] as QueryObserver[],
  retry: [] as Array<() => void>,
}))

vi.mock('../stores/authStore', () => ({
  useAuthStore: (select: (state: unknown) => unknown) => select({
    status: runtime.owner ? 'authed' : 'anonymous', user: runtime.owner ? { user_id: runtime.owner } : null,
  }),
}))
vi.mock('@tanstack/react-query', async (original) => {
  const actual = await original<typeof import('@tanstack/react-query')>()
  return { ...actual, useQuery: (options: QueryObserverOptions) => {
    const observer = new actual.QueryObserver(runtime.client!, options)
    runtime.observers.push(observer)
    return observer.getCurrentResult()
  } }
})
vi.mock('../api/monitorApi', () => ({ monitorApi: {
  summary: vi.fn(), services: vi.fn(), history: vi.fn(), alerts: vi.fn(),
} }))
vi.mock('antd', () => ({
  Space: 'div',
  Alert: ({ title, description, action }: { title: string; description: string; action: React.ReactNode }) =>
    createElement('section', {}, title, description, action),
  Button: ({ onClick, children }: { onClick: () => void; children: React.ReactNode }) => {
    runtime.retry.push(onClick)
    return createElement('button', {}, children)
  },
}))
vi.mock('../components/common/PageHeader', () => ({ PageHeader: () => createElement('h1', {}, '系统监控') }))
vi.mock('../components/common/PageLoading', () => ({ PageLoading: () => createElement('p', {}, 'loading') }))
vi.mock('../components/common/EmptyState', () => ({ EmptyState: ({ description }: { description: string }) => createElement('p', {}, description) }))
vi.mock('../components/monitor/MonitorMetricTable', () => ({ MonitorMetricTable: ({ snapshot }: { snapshot: unknown }) => createElement('p', {}, JSON.stringify(snapshot)) }))
vi.mock('../components/monitor/ServiceHealthPanel', () => ({ ServiceHealthPanel: ({ data }: { data: unknown }) => createElement('p', {}, JSON.stringify(data)) }))
vi.mock('../components/monitor/MonitorAlertTable', () => ({ MonitorAlertTable: ({ alerts }: { alerts: unknown }) => createElement('p', {}, JSON.stringify(alerts)) }))
vi.mock('../components/monitor/MonitorHistoryChart', () => ({ MonitorHistoryChart: ({ records }: { records: unknown }) => createElement('p', {}, JSON.stringify(records)) }))

const key = (section: string, owner = runtime.owner) => ['web', 'monitor', owner, section]
const render = () => renderToStaticMarkup(createElement(SystemMonitorPage))

function seed() {
  runtime.client!.setQueryData(key('summary'), { snapshot: { user: runtime.owner }, alerts: [{ alert_id: 'current', message: 'current alert' }] })
  runtime.client!.setQueryData(key('services'), { agent_runtime: { status: 'ok' } })
  runtime.client!.setQueryData(key('history'), { records: [] })
  runtime.client!.setQueryData(key('alerts'), { records: [{ alert_id: 'current', message: 'duplicate alert' }, { alert_id: 'old', message: 'historical alert' }] })
}

beforeEach(() => {
  vi.clearAllMocks()
  runtime.owner = 'alice'
  runtime.observers = []
  runtime.retry = []
  runtime.client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  seed()
})
afterEach(() => {
  runtime.observers.forEach((observer) => observer.destroy())
  runtime.client?.clear()
})

it('does not reuse another account monitor cache and disables reads after logout', () => {
  expect(render()).toContain('alice')
  runtime.owner = 'bob'
  expect(render()).not.toContain('alice')
  seed()
  expect(render()).toContain('bob')
  runtime.owner = undefined
  expect(render()).toContain('请登录')
  expect(runtime.observers.slice(-4).every((observer) => observer.options.enabled === false)).toBe(true)
})

it('shows current and historical alerts without duplicate IDs', () => {
  const html = render()
  expect(html).toContain('current alert')
  expect(html).toContain('historical alert')
  expect(html).not.toContain('duplicate alert')
})

it('shows partial failures and retries the failing endpoint while preserving the summary', async () => {
  await runtime.client!.fetchQuery({ queryKey: key('history'), queryFn: async () => { throw new Error('history unavailable') }, staleTime: 0 }).catch(() => undefined)
  const html = render()
  expect(html).toContain('alice')
  expect(html).toContain('监控历史加载失败')
  expect(html).toContain('history unavailable')
  vi.mocked(monitorApi.history).mockResolvedValue({ records: [], columns: [], total: 0 })
  runtime.retry[0]()
  await vi.waitFor(() => expect(monitorApi.history).toHaveBeenCalledOnce())
})

it('shows runtime collection failure returned inside a successful envelope', () => {
  runtime.client!.setQueryData(key('services'), { agent_runtime: { status: 'unavailable', error: 'RuntimeError' } })
  expect(render()).toContain('运行状态暂时不可用')
  expect(runtime.retry).toHaveLength(1)
})
