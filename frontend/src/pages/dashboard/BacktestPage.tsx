import { Alert, Card, Descriptions, Space, Tag } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { backtestApi } from '../../api/backtestApi'
import { BacktestMetricCards } from '../../components/dashboard/BacktestMetricCards'
import { BacktestNavChart } from '../../components/dashboard/BacktestNavChart'
import { BacktestTradeTable } from '../../components/dashboard/BacktestTradeTable'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { ReadOnlyNotice } from '../../components/common/ReadOnlyNotice'

export function BacktestPage() {
  const detail = useQuery({ queryKey: ['web', 'backtest', 'latest'], queryFn: () => backtestApi.detail() })
  const equity = useQuery({ queryKey: ['web', 'backtest', 'latest', 'equity'], queryFn: () => backtestApi.equity() })
  const trades = useQuery({ queryKey: ['web', 'backtest', 'latest', 'trades'], queryFn: () => backtestApi.trades() })
  if (detail.isLoading || equity.isLoading || trades.isLoading) return <PageLoading />
  if (detail.error || equity.error || trades.error) return <EmptyState title="回测数据加载失败" description={String(detail.error ?? equity.error ?? trades.error)} />
  if (!detail.data?.available) return <Space direction="vertical" size="large" style={{ width: '100%' }}><PageHeader title="回测分析" description="读取主动排名模型的独立留出回测结果。" /><ReadOnlyNotice /><Alert type="info" showIcon message="暂无当前主动模型回测结果" description="请先通过后端任务生成回测；页面不会复用旧模型结果。" /></Space>

  const metrics = detail.data.metrics ?? {}
  const targetMet = Boolean(metrics.target_met)
  return <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <PageHeader title="主动模型 Top15 回测" description="使用训练截止日之后的独立留出数据，按日选取融合排名前15名并计算下一交易日收益。" />
    <ReadOnlyNotice />
    <Alert type="info" showIcon message="回测口径" description="两个 LambdaRank 成员先分别计算每日截面百分位秩，再等权融合；按信号日收盘建立等权 Top15，持有至下一交易日收盘，并计入买卖费用和印花税。融合分不是上涨概率。" />
    <Card title="回测身份与区间">
      <Descriptions size="small" column={{ xs: 1, md: 2, xl: 4 }}>
        <Descriptions.Item label="模型">{String(metrics.model_name ?? '—')}</Descriptions.Item>
        <Descriptions.Item label="版本">{String(metrics.model_version ?? '—')}</Descriptions.Item>
        <Descriptions.Item label="区间">{String(metrics.start_date ?? '—')} 至 {String(metrics.end_date ?? '—')}</Descriptions.Item>
        <Descriptions.Item label="完整交易日">{String(metrics.periods ?? 0)}</Descriptions.Item>
        <Descriptions.Item label="每日选股">Top {String(metrics.topk ?? 15)}</Descriptions.Item>
        <Descriptions.Item label="精确率目标"><Tag color={targetMet ? 'success' : 'warning'}>{targetMet ? '已达到 55%' : '尚未达到 55%'}</Tag></Descriptions.Item>
      </Descriptions>
    </Card>
    <BacktestMetricCards metrics={metrics} />
    <BacktestNavChart records={equity.data?.records ?? []} />
    <BacktestTradeTable records={trades.data?.records ?? []} />
  </Space>
}
