import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Space } from 'antd'
import { paperTradingApi } from '../../api/paperTradingApi'
import { queryKeys } from '../../api/queryKeys'
import { settingsApi } from '../../api/settingsApi'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { EmptyState } from '../../components/common/EmptyState'
import { AccountSummary } from '../../components/paper/AccountSummary'
import { AssetCurve } from '../../components/paper/AssetCurve'
import { CashFlowPanel } from '../../components/paper/CashFlowPanel'
import { DailyHistoryPanel } from '../../components/paper/DailyHistoryPanel'
import { BackfillPanel } from '../../components/paper/BackfillPanel'
import { PaperTables, RiskAndDiagnostics } from '../../components/paper/PaperTables'
import { PaperTaskActions } from '../../components/paper/PaperTaskActions'
import { ProposalPanel } from '../../components/paper/ProposalPanel'
import { UserContextSelector } from '../../components/paper/UserContextSelector'
import { UserProfileForm } from '../../components/paper/UserProfileForm'
import { PaperSectionControls, PaperSectionProvider } from '../../components/paper/PaperSectionCard'
import { normalizeOwnerId, resolveOwnerId } from '../../stores/sessionIdentity'
import { useSessionStore } from '../../stores/sessionStore'

export function PaperTradingPage() {
  const storedOwnerId = useSessionStore((state) => state.ownerId)
  const ownerMode = useSessionStore((state) => state.ownerMode)
  const setOwnerId = useSessionStore((state) => state.setOwnerId)
  const useSystemOwnerId = useSessionStore((state) => state.useSystemOwnerId)

  const settings = useQuery({
    queryKey: ['web', 'settings', 'paper-user-context'],
    queryFn: settingsApi.get,
    staleTime: 60_000,
  })
  const systemUserId = normalizeOwnerId(settings.data?.current_user_id)
  const userId = resolveOwnerId(storedOwnerId, ownerMode, systemUserId)
  const identityReady = ownerMode === 'manual' || settings.isSuccess || settings.isError

  useEffect(() => {
    if (ownerMode === 'system' && settings.data && storedOwnerId !== systemUserId) {
      useSystemOwnerId(systemUserId)
    }
  }, [ownerMode, settings.data, storedOwnerId, systemUserId, useSystemOwnerId])

  const snapshot = useQuery({
    queryKey: queryKeys.paperTrading(userId),
    queryFn: () => paperTradingApi.summary(userId),
    refetchInterval: 15_000,
    enabled: identityReady,
  })
  const proposals = useQuery({
    queryKey: queryKeys.paperProposals(userId),
    queryFn: () => paperTradingApi.proposals(userId),
    refetchInterval: 10_000,
    enabled: identityReady,
  })

  if (!identityReady || snapshot.isLoading) return <PageLoading />
  if (snapshot.error || !snapshot.data) {
    return <EmptyState title="AI 模拟盘加载失败" description={String(snapshot.error ?? '无数据')} />
  }

  const data = snapshot.data
  const modelContext = data.model_context ?? {}
  const historyDates = [
    ...data.position_snapshot_dates,
    ...data.order_snapshot_dates,
  ].map((value) => String(value ?? '')).filter(Boolean)
  return (
    <PaperSectionProvider>
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        <div className="paper-page-heading-row">
          <PageHeader title="AI 模拟盘" description="模拟盘状态、受保护写操作和长任务统一通过 FastAPI；不连接真实交易。" />
          <PaperSectionControls />
        </div>
      {settings.error ? (
        <Alert
          type="warning"
          showIcon
          message="系统当前用户读取失败，暂时使用 default"
          description={String(settings.error)}
        />
      ) : null}
      <UserContextSelector
        activeUserId={userId}
        systemUserId={systemUserId}
        ownerMode={ownerMode}
        onSelectUser={setOwnerId}
        onUseSystemUser={() => useSystemOwnerId(systemUserId)}
      />
      <Alert
        type="warning"
        showIcon
        message="仅用于模拟和项目展示，不构成投资建议"
        description="所有资金、持仓和画像变更均以服务端为唯一真相；受保护写操作需要预览、二次确认和重新校验。"
      />
      <Alert
        type={modelContext.account_stale ? 'warning' : 'success'}
        showIcon
        message={modelContext.account_stale ? '模拟盘快照落后于最新模型排名' : '模拟盘已对齐最新模型排名'}
        description={`主动模型 ${String(modelContext.model_name ?? '—')} · 排名信号日 ${String(modelContext.ranking_signal_date ?? '—')} · 预测目标日 ${String(modelContext.ranking_prediction_date ?? '—')} · 账户更新时间 ${String(modelContext.account_updated_at ?? '—')}${modelContext.account_stale ? '。可使用“更新 AI 模拟盘”通过 Task API 生成并执行新的模拟盘计划。' : ''}`}
      />
      <AccountSummary account={data.account} available={data.is_available} />
      <PaperTaskActions profileComplete={data.profile_complete} />
      <UserProfileForm userId={userId} profile={data.profile} options={data.profile_options} complete={data.profile_complete} />
      <AssetCurve data={data.nav_history} />
      <DailyHistoryPanel userId={userId} availableDates={historyDates} />
      <PaperTables positions={data.positions} orders={data.orders} decisions={data.decisions} cashFlows={data.cash_flows} />
      <RiskAndDiagnostics risk={data.risk_report} diagnostics={data.execution_diagnostics} settings={data.trading_settings} />
      <CashFlowPanel userId={userId} />
      <BackfillPanel userId={userId} />
        <ProposalPanel userId={userId} proposals={proposals.data?.records ?? []} />
      </Space>
    </PaperSectionProvider>
  )
}
