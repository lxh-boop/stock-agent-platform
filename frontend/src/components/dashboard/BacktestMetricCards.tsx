import { Card, Statistic } from 'antd'
import { MetricGrid } from '../common/MetricGrid'

type MetricSpec = {
  key: string
  title: string
  percent?: boolean
}

const METRICS: MetricSpec[] = [
  { key: 'cumulative_return', title: '策略累计收益', percent: true },
  { key: 'benchmark_cumulative_return', title: '股票池同期收益', percent: true },
  { key: 'annualized_return', title: '年化收益', percent: true },
  { key: 'max_drawdown', title: '最大回撤', percent: true },
  { key: 'sharpe_ratio', title: '夏普比率' },
  { key: 'information_ratio', title: '信息比率' },
  { key: 'win_rate', title: '组合日胜率', percent: true },
  { key: 'topk_daily_average_up_rate', title: 'Top15 上涨精确率', percent: true },
]

export function BacktestMetricCards({ metrics }: { metrics: Record<string, unknown> }) {
  const shown = METRICS.filter(({ key }) => metrics[key] !== null && metrics[key] !== undefined && metrics[key] !== '' && Number.isFinite(Number(metrics[key])))
  return <MetricGrid>{shown.map(({ key, title, percent }) => {
    const value = Number(metrics[key])
    return <Card key={key}><Statistic title={title} value={percent ? value * 100 : value} precision={2} suffix={percent ? '%' : undefined} /></Card>
  })}</MetricGrid>
}
