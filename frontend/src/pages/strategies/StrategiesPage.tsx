import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Drawer, Empty, Modal, Pagination, Space, Statistic, Typography, message } from 'antd'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { strategiesApi } from '../../api/strategiesApi'
import { backtestApi } from '../../api/backtestApi'
import { errorMessage } from '../../api/errors'
import { cancelTask, getTask } from '../../api/taskApi'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { TaskProgress } from '../../components/tasks/TaskProgress'
import { ResearchReport } from '../../components/dashboard/ResearchReport'
import { NewStrategyModal } from '../../components/strategies/NewStrategyModal'
import { SaveStrategyFromReport } from '../../components/strategies/SaveStrategyFromReport'
import { StrategyStatus, StrategySummaryCard } from '../../components/strategies/StrategySummaryCard'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import { useTaskStore } from '../../stores/taskStore'
import { useAuthStore } from '../../stores/authStore'
import type { StrategyWorkspaceItem } from '../../types/strategies'
import { TERMINAL_TASK_STATUSES, type TaskRecord } from '../../types/task'
import { backtestRequestId } from '../../utils/backtest'
import { benchmarkLabels, strategyIsRunning, strategyNumber, strategyPercent } from '../../utils/strategies'

function StrategyTaskPanel({ initialTask, owner }: { initialTask: TaskRecord; owner: string }) {
  const client = useQueryClient(), setTask = useTaskStore(state => state.setTask)
  const liveTask = useTaskStore(state => state.task)
  const completed = useRef('')
  const query = useQuery({
    queryKey: ['strategy-task', owner, initialTask.task_id],
    queryFn: () => getTask(initialTask.task_id), initialData: initialTask,
    refetchInterval: result => result.state.data && TERMINAL_TASK_STATUSES.has(result.state.data.status) ? false : 1500,
    retry: 1,
  })
  // RuntimeBridge subscribes to this task's SSE. The authenticated server record
  // restores it after a reload; polling covers a dropped event stream.
  useEffect(() => {
    if (initialTask.owner_id === owner && !TERMINAL_TASK_STATUSES.has(initialTask.status)) setTask(initialTask)
  }, [initialTask.task_id, owner, setTask])
  useEffect(() => {
    const current = useTaskStore.getState().task
    if (useAuthStore.getState().user?.user_id !== owner || !query.data || query.data.owner_id !== owner) return
    if (current?.task_id === initialTask.task_id && !(TERMINAL_TASK_STATUSES.has(current.status) && !TERMINAL_TASK_STATUSES.has(query.data.status))) setTask(query.data)
  }, [query.data, owner, initialTask.task_id, setTask])
  const task = liveTask?.task_id === initialTask.task_id && liveTask.owner_id === owner
    ? (query.data && TERMINAL_TASK_STATUSES.has(query.data.status) ? query.data : liveTask)
    : query.data ?? initialTask
  useEffect(() => {
    if (!TERMINAL_TASK_STATUSES.has(task.status) || completed.current === task.task_id) return
    completed.current = task.task_id
    void client.invalidateQueries({ queryKey: ['strategies', owner] })
    void client.invalidateQueries({ queryKey: ['backtests', owner] })
  }, [task.status, task.task_id, client, owner])
  const cancel = useMutation({
    mutationFn: () => cancelTask(task.task_id),
    onSuccess: value => {
      if (useAuthStore.getState().user?.user_id !== owner) return
      client.setQueryData(['strategy-task', owner, task.task_id], value)
      if (useTaskStore.getState().activeTaskId === task.task_id) setTask(value)
      void client.invalidateQueries({ queryKey: ['strategies', owner] })
    },
    onError: error => message.error(errorMessage(error)),
  })
  return <Card title="策略回测任务" className="strategy-task-panel">
    <TaskProgress task={task} />
    {query.error && <Alert type="warning" showIcon message="任务状态暂时读取失败，正在保留最后一次已知状态" description={errorMessage(query.error)} action={<Button onClick={() => void query.refetch()}>重试查询</Button>} />}
    {!TERMINAL_TASK_STATUSES.has(task.status) && <Button danger loading={cancel.isPending} disabled={task.status === 'cancelling'} onClick={() => cancel.mutate()}>取消本次回测</Button>}
  </Card>
}

export function StrategiesPage() {
  const owner = useCurrentUserId(), navigate = useNavigate(), client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const selectedId = params.get('strategy') ?? '', directReportId = params.get('report') ?? ''
  const [page, setPage] = useState(1), [showNew, setShowNew] = useState(false)
  const [submitted, setSubmitted] = useState<{ strategyId: string; task: TaskRecord } | null>(null)
  const requests = useRef(new Map<string, { version: number; id: string }>())
  const deleteRequests = useRef(new Map<string, { version: number; id: string }>())
  useEffect(() => { setPage(1); setShowNew(false); setSubmitted(null); requests.current.clear(); deleteRequests.current.clear() }, [owner])

  const catalog = useQuery({ queryKey: ['backtests', owner, 'catalog'], queryFn: backtestApi.catalog, enabled: Boolean(owner) })
  const list = useQuery({ queryKey: ['strategies', owner, 'list', page], queryFn: () => strategiesApi.list((page - 1) * 12, 12),
    enabled: Boolean(owner), refetchInterval: result => result.state.data?.stats.running ? 3000 : 15000 })
  const detail = useQuery({ queryKey: ['strategies', owner, 'detail', selectedId], queryFn: () => strategiesApi.detail(selectedId),
    enabled: Boolean(owner && selectedId), refetchInterval: result => result.state.data && strategyIsRunning(result.state.data) ? 3000 : 15000 })
  const selected = detail.data
  const reportId = directReportId || selected?.latest_run_id || ''
  const report = useQuery({ queryKey: ['backtests', owner, reportId, 'detail'], queryFn: () => backtestApi.detail(reportId),
    enabled: Boolean(owner && reportId && (selectedId || directReportId)) })
  const rows = list.data?.records ?? []
  const task = selected?.task ?? submitted?.task ?? rows.find(item => item.task && !TERMINAL_TASK_STATUSES.has(item.task.status))?.task
  const refresh = async () => { await client.invalidateQueries({ queryKey: ['strategies', owner] }) }
  const rerun = useMutation({
    mutationFn: async (strategy: StrategyWorkspaceItem) => {
      let pending = requests.current.get(strategy.strategy_id)
      if (!pending || pending.version !== strategy.version) {
        pending = { version: strategy.version, id: backtestRequestId() }
        requests.current.set(strategy.strategy_id, pending)
      }
      return strategiesApi.rerun(strategy.strategy_id, pending.id, strategy.version)
    },
    onSuccess: async (value, strategy) => {
      if (useAuthStore.getState().user?.user_id !== owner || value.owner_id !== owner) return
      requests.current.delete(strategy.strategy_id)
      setSubmitted({ strategyId: strategy.strategy_id, task: value })
      message.success('回测任务已提交，可离开页面后继续查看进度')
      await refresh()
    },
    onError: error => message.error(errorMessage(error)),
  })
  const beginRerun = (strategy: StrategyWorkspaceItem) => Modal.confirm({
    title: `重新回测「${strategy.spec.name}」？`, okText: '确认运行',
    content: <Space direction="vertical"><Typography.Text>使用已保存的版本 {strategy.version}：{strategy.spec.start_date} 至 {strategy.spec.end_date}，最多 {strategy.spec.max_positions} 只股票。</Typography.Text>
      <Typography.Text>独立研究资金 {strategy.spec.initial_cash.toLocaleString('zh-CN')} 元，{strategy.spec.execution === 'open' ? '次日开盘' : '次日收盘'}成交，买入费率 {strategyPercent(strategy.spec.buy_fee)}，卖出费率 {strategyPercent(strategy.spec.sell_fee)}。</Typography.Text>
      <Typography.Text type="secondary">生成新的研究报告；已有模拟账户和持仓保持原样。</Typography.Text></Space>,
    // onError shows the failure. Resolve the static dialog's promise so Antd
    // does not rethrow it globally; the request ID remains available for retry.
    onOk: () => rerun.mutateAsync(strategy).then(() => undefined).catch(() => undefined),
  })
  const remove = useMutation({
    mutationFn: ({ id, requestId, version }: { id: string; requestId: string; version: number }) => strategiesApi.remove(id, requestId, version),
    onSuccess: async (_, variables) => {
      if (useAuthStore.getState().user?.user_id !== owner) return
      deleteRequests.current.delete(variables.id)
      if (selectedId === variables.id) setParams({})
      if (submitted?.strategyId === variables.id) setSubmitted(null)
      message.success('策略已删除，历史报告和模拟账户保留')
      if (rows.length === 1 && page > 1) setPage(page - 1)
      await refresh()
      void client.invalidateQueries({ queryKey: ['backtests', owner, 'strategies'] })
    },
    onError: error => message.error(errorMessage(error)),
  })
  const beginDelete = (strategy: StrategyWorkspaceItem) => {
    let pending = deleteRequests.current.get(strategy.strategy_id)
    if (!pending || pending.version !== strategy.version) {
      pending = { version: strategy.version, id: backtestRequestId() }
      deleteRequests.current.set(strategy.strategy_id, pending)
    }
    const requestId = pending.id
    Modal.confirm({ title: `删除策略「${strategy.spec.name}」？`, content: '仅删除这个策略定义，操作不可撤销。已有研究报告、模拟账户资金及持仓不会删除。',
      okText: '确认删除', okButtonProps: { danger: true }, onOk: () => remove.mutateAsync({ id: strategy.strategy_id, requestId, version: strategy.version }).then(() => undefined).catch(() => undefined) })
  }
  const editInAgent = (strategy: StrategyWorkspaceItem) => navigate('/agent', { state: { strategyDraft: {
    prefillId: backtestRequestId(), text: `我想调整已保存策略「${strategy.spec.name}」（strategy_id=${strategy.strategy_id}，当前版本 ${strategy.version}）。请先读取当前策略规格和最近报告，询问我希望修改哪些规则。确认修改后再保存或回测，不更改已有模拟账户。`,
  } } })

  const stats = list.data?.stats
  return <Space direction="vertical" size="large" className="strategies-page">
    <div className="strategies-heading"><PageHeader title="我的策略" description="管理个人因子策略，比较真实回测结果，在 AI Agent 中创建和调整。" />
      <Space wrap><Button onClick={() => navigate('/backtests')}>高级研究</Button><Button onClick={() => navigate('/paper-account')}>历史模拟账户</Button>
        <Button type="primary" disabled={!catalog.data?.coverage} onClick={() => setShowNew(true)}>新建策略</Button></Space>
    </div>
    <div className="strategy-stats">
      <Card><Statistic title="策略总数" value={stats?.total ?? '—'} suffix={stats ? '个' : undefined} /></Card>
      <Card><Statistic title="回测运行中" value={stats?.running ?? '—'} suffix={stats ? '个' : undefined} /></Card>
      <Card><Statistic title="平均年化收益" value={strategyPercent(stats?.avg_annual_return)} /><Typography.Text type="secondary">本人策略的最近成功报告</Typography.Text></Card>
      <Card><Statistic title="平均夏普比率" value={strategyNumber(stats?.avg_sharpe)} /><Typography.Text type="secondary">不代表组合整体收益</Typography.Text></Card>
    </div>
    {catalog.error ? <Alert type="warning" showIcon message="可用因子信息加载失败" description={errorMessage(catalog.error)} action={<Button onClick={() => void catalog.refetch()}>重试</Button>} />
      : catalog.data?.coverage ? <Typography.Text type="secondary">本人数据覆盖 {catalog.data.coverage.start_date} ～ {catalog.data.coverage.end_date}，{catalog.data.coverage.stock_count} 只股票。</Typography.Text>
        : <Typography.Text type="secondary">{catalog.data?.message || '正在读取本人因子库…'} <Link to="/paper-account">前往账户运行日更</Link></Typography.Text>}
    {task && task.owner_id === owner && <StrategyTaskPanel key={task.task_id} initialTask={task} owner={owner} />}
    <div className="strategies-list-heading"><Typography.Title level={3}>策略库</Typography.Title><Button onClick={() => void refresh()} loading={list.isFetching}>刷新策略</Button></div>
    {list.error ? <Alert type="error" showIcon message="策略列表加载失败" description={errorMessage(list.error)} action={<Button onClick={() => void list.refetch()}>重试加载</Button>} />
      : list.isLoading ? <PageLoading />
        : !rows.length ? <Card><Empty description="还没有保存的策略"><Typography.Paragraph type="secondary">在 AI Agent 中完成回测后，将真实报告保存到这里。已有模拟账户可以从“历史模拟账户”查看。</Typography.Paragraph><Button type="primary" disabled={!catalog.data?.coverage} onClick={() => setShowNew(true)}>新建策略</Button></Empty></Card>
          : <><div className="strategy-grid">{rows.map(strategy => <StrategySummaryCard key={strategy.strategy_id} strategy={strategy}
            busy={rerun.isPending && rerun.variables?.strategy_id === strategy.strategy_id}
            onOpen={() => setParams({ strategy: strategy.strategy_id })} onRerun={() => beginRerun(strategy)} onDelete={() => beginDelete(strategy)} />)}</div>
            <Pagination current={page} pageSize={12} total={list.data?.total ?? 0} onChange={setPage} showSizeChanger={false} hideOnSinglePage /></>}
    <Typography.Text type="secondary">本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。</Typography.Text>
    {showNew && <NewStrategyModal catalog={catalog.data} onClose={() => setShowNew(false)} onDraft={text => {
      setShowNew(false); navigate('/agent', { state: { strategyDraft: { text, prefillId: backtestRequestId() } } })
    }} />}
    <Drawer open={Boolean(selectedId || directReportId)} onClose={() => setParams({})} title={selected?.spec.name ?? (directReportId ? '研究报告' : '策略详情')}
      size="large" className="strategy-detail-drawer">
      {selectedId && detail.isLoading ? <PageLoading /> : selectedId && detail.error ? <Alert type="error" showIcon message="策略详情加载失败" description={errorMessage(detail.error)} action={<Button onClick={() => void detail.refetch()}>重试</Button>} /> : <Space direction="vertical" size="large" style={{ width: '100%' }}>
        {selected && <>
          <Space wrap><StrategyStatus status={selected.status} /><Typography.Text>配置版本 {selected.version}</Typography.Text></Space>
          <Descriptions size="small" column={1} bordered items={[
            { key: 'dates', label: '保存的回测区间', children: `${selected.spec.start_date} ～ ${selected.spec.end_date}` },
            { key: 'pool', label: '股票范围', children: selected.spec.stock_codes?.join('、') || '本人数据库覆盖股票' },
            { key: 'cash', label: '研究资金', children: `${selected.spec.initial_cash.toLocaleString('zh-CN')} 元` },
            { key: 'factor', label: '因子', children: selected.spec.factors.map(f => `${f.field}（${f.direction === 1 ? '值大优先' : '值小优先'}）`).join('、') },
            { key: 'schedule', label: '持仓与调仓', children: `最多 ${selected.spec.max_positions} 只，${selected.spec.rebalance_days ? `每 ${selected.spec.rebalance_days} 日调仓` : '条件触发'}` },
            { key: 'benchmark', label: '参考基准', children: benchmarkLabels[selected.spec.benchmark ?? 'pool'] },
          ]} />
          <Space wrap><Button type="primary" disabled={strategyIsRunning(selected)} loading={rerun.isPending} onClick={() => beginRerun(selected)}>重新回测</Button>
            <Button onClick={() => editInAgent(selected)}>在 AI Agent 中调整</Button><Button onClick={() => navigate(`/backtests?strategy=${encodeURIComponent(selected.strategy_id)}`)}>高级参数编辑</Button>
            <Button danger disabled={strategyIsRunning(selected)} loading={remove.isPending} onClick={() => beginDelete(selected)}>删除策略</Button></Space>
          {selected.error && <Alert type="error" showIcon message="最近一次回测未完成" description={selected.error} />}
          {selected.latest_report && <Typography.Title level={4}>最近成功回测 · 版本 {selected.latest_run_version ?? '—'}</Typography.Title>}
          {selected.latest_report && selected.latest_run_version !== selected.version && <Alert type="warning" showIcon message="当前配置已修改，下面展示的是旧版本报告。重新回测后才会更新当前版本的结果。" />}
        </>}
        {report.isLoading && reportId ? <PageLoading /> : report.error ? <Alert type="error" showIcon message="报告加载失败" description={errorMessage(report.error)} action={<Button onClick={() => void report.refetch()}>重试报告</Button>} />
          : report.data ? <><SaveStrategyFromReport key={report.data.run_id} report={report.data} onSaved={strategy => setParams({ strategy: strategy.strategy_id })} /><ResearchReport key={report.data.run_id} data={report.data} owner={owner} /></>
            : selected && <Empty description="还没有关联的成功回测报告。可以按已保存参数运行回测。" />}
      </Space>}
    </Drawer>
  </Space>
}
