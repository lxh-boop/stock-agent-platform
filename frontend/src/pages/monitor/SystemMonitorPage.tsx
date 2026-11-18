import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Space } from 'antd'
import { monitorApi } from '../../api/monitorApi'
import { errorMessage } from '../../api/errors'
import { useAuthStore } from '../../stores/authStore'
import { MonitorAlertTable } from '../../components/monitor/MonitorAlertTable'
import { MonitorHistoryChart } from '../../components/monitor/MonitorHistoryChart'
import { ServiceHealthPanel } from '../../components/monitor/ServiceHealthPanel'
import { MonitorMetricTable } from '../../components/monitor/MonitorMetricTable'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'

function LoadError({ title, error, retry }: { title: string; error: unknown; retry: () => void }) {
  return <Alert type="error" showIcon title={title} description={errorMessage(error)}
    action={<Button onClick={retry}>重试</Button>} />
}

export function SystemMonitorPage() {
  const owner = useAuthStore((state) => state.status === 'authed' ? state.user?.user_id : undefined)
  const enabled = Boolean(owner)
  const summary = useQuery({ queryKey: ['web', 'monitor', owner, 'summary'], queryFn: () => monitorApi.summary(), enabled })
  const services = useQuery({ queryKey: ['web', 'monitor', owner, 'services'], queryFn: () => monitorApi.services(), enabled })
  const history = useQuery({ queryKey: ['web', 'monitor', owner, 'history'], queryFn: () => monitorApi.history(), enabled })
  const alerts = useQuery({ queryKey: ['web', 'monitor', owner, 'alerts'], queryFn: () => monitorApi.alerts(), enabled })
  if (!owner) return <EmptyState description="请登录后查看系统监控" />
  if (summary.isPending) return <PageLoading />
  const currentAlerts = summary.data?.alerts ?? []
  const currentIds = new Set(currentAlerts.map((alert) => alert.alert_id).filter(Boolean))
  const recordedAlerts = (alerts.data?.records ?? []).filter((alert) => !currentIds.has(alert.alert_id))
  const runtimeHealth = services.data?.agent_runtime
  return <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <PageHeader title="系统监控" description="" />
    {summary.error
      ? <LoadError title="系统监控加载失败" error={summary.error} retry={() => { void summary.refetch() }} />
      : <MonitorMetricTable snapshot={summary.data?.snapshot ?? {}} />}
    {services.error
      ? <LoadError title="运行状态加载失败" error={services.error} retry={() => { void services.refetch() }} />
      : services.isPending ? <PageLoading />
        : runtimeHealth?.status === 'unavailable'
          ? <LoadError title="运行状态暂时不可用" error="暂时无法读取运行记录，请稍后重试。" retry={() => { void services.refetch() }} />
          : <ServiceHealthPanel title="Agent 运行状态" data={runtimeHealth} />}
    {alerts.error && <LoadError title="历史告警加载失败" error={alerts.error} retry={() => { void alerts.refetch() }} />}
    <MonitorAlertTable alerts={[...currentAlerts, ...recordedAlerts]} />
    {history.error
      ? <LoadError title="监控历史加载失败" error={history.error} retry={() => { void history.refetch() }} />
      : history.isPending ? <PageLoading /> : <MonitorHistoryChart records={history.data?.records ?? []} />}
  </Space>
}
