import { Button, Card, Space, Tag, Typography } from 'antd'
import type { StrategyEquityPoint, StrategyWorkspaceItem } from '../../types/strategies'
import { benchmarkLabels, strategyFactorLabel, strategyIsRunning, strategyNumber, strategyPercent } from '../../utils/strategies'
import { formatTaskTime } from '../../utils/taskTime'

const statuses: Record<StrategyWorkspaceItem['status'], { label: string; color: string }> = {
  draft: { label: '待回测', color: 'default' }, queued: { label: '排队中', color: 'processing' },
  running: { label: '回测中', color: 'processing' }, cancelling: { label: '正在取消', color: 'warning' },
  succeeded: { label: '回测完成', color: 'success' }, failed: { label: '回测失败', color: 'error' },
  cancelled: { label: '已取消', color: 'default' }, timed_out: { label: '已超时', color: 'error' },
  interrupted: { label: '已中断', color: 'warning' },
}

export function StrategyStatus({ status }: { status: StrategyWorkspaceItem['status'] }) {
  const item = statuses[status]
  return <Tag color={item.color}>{item.label}</Tag>
}

export function StrategySparkline({ rows }: { rows: StrategyEquityPoint[] }) {
  const valid = rows.map(row => row.nav).filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
  if (rows.length < 2 || valid.length < 2) return <div className="strategy-card-chart strategy-chart-empty">暂无真实回测曲线</div>
  const low = Math.min(...valid), high = Math.max(...valid), span = high - low || 1
  let connected = false
  const path = rows.map((row, index) => {
    if (typeof row.nav !== 'number' || !Number.isFinite(row.nav)) { connected = false; return '' }
    const point = `${connected ? 'L' : 'M'}${8 + index / (rows.length - 1) * 584},${82 - (row.nav - low) / span * 70}`
    connected = true
    return point
  }).join(' ')
  return <div className="strategy-card-chart"><svg viewBox="0 0 600 92" role="img" aria-label="策略历史净值">
    <path d={path} fill="none" stroke="currentColor" strokeWidth="2.5" />
  </svg><div className="strategy-chart-dates"><span>{rows[0].date}</span><span>{rows.at(-1)?.date}</span></div></div>
}

export function StrategySummaryCard({ strategy, busy, onOpen, onRerun, onDelete }: {
  strategy: StrategyWorkspaceItem; busy: boolean; onOpen: () => void; onRerun: () => void; onDelete: () => void
}) {
  const running = strategyIsRunning(strategy)
  const report = strategy.latest_report
  const metrics = report?.metrics
  const oldVersion = report && strategy.latest_run_version !== strategy.version
  return <Card className="strategy-summary-card" title={<Button type="link" className="strategy-card-title" onClick={onOpen}>{strategy.spec.name}</Button>}
    extra={<StrategyStatus status={strategy.status} />}>
    <Typography.Paragraph type="secondary" className="strategy-card-subtitle">
      {strategy.spec.stock_codes?.length ? `${strategy.spec.stock_codes.length} 只指定股票` : '本人数据库股票池'}
      {' · '}{strategy.spec.rebalance_days ? `每 ${strategy.spec.rebalance_days} 日调仓` : '条件触发调仓'}
      {' · '}{benchmarkLabels[strategy.spec.benchmark ?? 'pool']}
    </Typography.Paragraph>
    <Space wrap size={[4, 6]}>{strategy.spec.factors.map((factor, index) => <Tag key={factor.field}>{strategyFactorLabel(strategy.spec, index)}</Tag>)}</Space>
    <div className="strategy-report-caption">{report ? `上次成功回测 · 版本 ${strategy.latest_run_version ?? '—'}${oldVersion ? '（当前配置待回测）' : ''}` : '尚无已完成的回测报告'}</div>
    <div className="strategy-card-metrics">
      {[['年化收益', strategyPercent(metrics?.annual_return)], ['最大回撤', strategyPercent(metrics?.max_drawdown)],
        ['夏普比率', strategyNumber(metrics?.sharpe)], ['卖出笔胜率', strategyPercent(metrics?.sell_win_rate)]].map(([label, value]) =>
        <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
    </div>
    <StrategySparkline rows={report?.equity ?? []} />
    {strategy.error && <Typography.Paragraph type="danger" ellipsis={{ rows: 2, tooltip: true }}>{strategy.error}</Typography.Paragraph>}
    <div className="strategy-card-footer"><Typography.Text type="secondary">更新于 {formatTaskTime(strategy.updated_at)}</Typography.Text>
      <Space wrap><Button aria-label="详情" onClick={onOpen}>详情</Button><Button onClick={onRerun} loading={busy} disabled={running}>{strategy.status === 'failed' || strategy.status === 'timed_out' ? '重试回测' : '重新回测'}</Button>
        <Button aria-label="删除" danger onClick={onDelete} disabled={running || busy}>删除</Button></Space>
    </div>
  </Card>
}
