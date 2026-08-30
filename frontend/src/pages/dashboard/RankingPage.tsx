import { useQuery } from '@tanstack/react-query'
import { Card, Col, Row, Space, Statistic, Table, Tag, Typography } from 'antd'
import { useNavigate } from 'react-router-dom'
import { dashboardApi } from '../../api/dashboardApi'
import { ModelStatusCards } from '../../components/dashboard/ModelStatusCards'
import { RankingScoreChart } from '../../components/dashboard/RankingScoreChart'
import { RankingTable } from '../../components/dashboard/RankingTable'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { ReadOnlyNotice } from '../../components/common/ReadOnlyNotice'

export function RankingPage() {
  const navigate = useNavigate()
  const summary = useQuery({ queryKey: ['web','dashboard','summary'], queryFn: dashboardApi.summary })
  const rankings = useQuery({ queryKey: ['web','dashboard','rankings'], queryFn: () => dashboardApi.rankings(0, 300) })
  const freshness = useQuery({ queryKey: ['web','dashboard','freshness'], queryFn: dashboardApi.dataFreshness })
  if (summary.isLoading || rankings.isLoading) return <PageLoading />
  if (summary.error || rankings.error) return <EmptyState title="只读首页加载失败" description={String(summary.error ?? rankings.error)} />
  const records = rankings.data?.records ?? []
  const top15 = rankings.data?.top15_statistics
  const target = rankings.data?.target_validation
  const liftText = (value?: number) => typeof value === 'number' ? `${value >= 0 ? '+' : ''}${(value * 100).toFixed(2)} 个百分点` : '—'
  const percentage = (value?: number) => Number(((value ?? 0) * 100).toFixed(2))
  return <Space direction="vertical" size="large" style={{width:'100%'}}>
    <PageHeader title="首页 / 预测排名" description="展示主动截面排序模型的下一交易日 Top15；排名由两个已注册模型的当日截面百分位秩等权融合得到。排序分不是个股上涨概率。" />
    <ReadOnlyNotice />
    {summary.data && <ModelStatusCards summary={summary.data}/>}
    <Card title={`最新预测排名 · ${rankings.data?.total ?? 0} 条`}><RankingTable records={records} onSelect={(code)=>navigate(`/stocks/${code}`)}/></Card>
    <Card title="独立留出集验证">
      {typeof target?.universe_next_day_up_probability === 'number' ? <>
        <Row gutter={[16, 16]}>
          <Col xs={12} lg={6}><Statistic title="同期股票池上涨基准" value={percentage(target.universe_next_day_up_probability)} precision={2} suffix="%" /></Col>
          <Col xs={12} lg={6}><Statistic title={`Top5（${liftText(target.top5_lift_vs_universe)}）`} value={percentage(target.top5_next_day_up_probability)} precision={2} suffix="%" /></Col>
          <Col xs={12} lg={6}><Statistic title={`Top10（${liftText(target.top10_lift_vs_universe)}）`} value={percentage(target.top10_next_day_up_probability)} precision={2} suffix="%" /></Col>
          <Col xs={12} lg={6}><Statistic title={`Top15（${liftText(target.top15_lift_vs_universe)}）`} value={percentage(target.top15_next_day_up_probability)} precision={2} suffix="%" /></Col>
        </Row>
        <Typography.Text type="secondary">
          训练数据截止 {target.train_end_date || '—'}；测试区间 {target.test_start_date || '—'} 至 {target.test_end_date || '—'}，共 {target.valid_test_days ?? 0} 个未参与训练的交易日。
        </Typography.Text>
        <div style={{marginTop: 8}}><Tag color={target.target_met ? 'success' : 'warning'}>{target.target_met ? '达到 55% 目标' : 'Top15 尚未达到 55%，当前使用用户指定的最佳可用模型'}</Tag></div>
      </> : <Typography.Text type="secondary">暂无目标模式未见数据验证结果。</Typography.Text>}
    </Card>
    <Card title="历史每日排名前5名 / 前10名 / 前15名上涨统计">
      {typeof top15?.daily_average_up_rate === 'number' ? <>
        <Row gutter={[16, 16]}>
          <Col xs={12} lg={6}><Statistic title="前5名下一交易日上涨概率" value={Number(top15.top5_daily_average_up_rate ?? 0) * 100} precision={2} suffix="%" /></Col>
          <Col xs={12} lg={6}><Statistic title="前10名下一交易日上涨概率" value={Number(top15.top10_daily_average_up_rate ?? 0) * 100} precision={2} suffix="%" /></Col>
          <Col xs={12} lg={6}><Statistic title="前15名下一交易日上涨概率" value={top15.daily_average_up_rate * 100} precision={2} suffix="%" /></Col>
          <Col xs={12} lg={6}><Statistic title="完整交易日" value={top15.complete_days ?? top15.observation_days ?? 0} /></Col>
        </Row>
        <Typography.Text type="secondary">
          统计区间 {top15.start_date || '—'} 至 {top15.end_date || '—'}，前15名共有 {Number(top15.observation_count ?? 0).toLocaleString()} 个有效样本、{Number(top15.rise_count ?? 0).toLocaleString()} 次实际上涨。每天分别截取当日模型排名第1–5名、第1–10名和第1–15名，计算各组股票下一交易日的实际上涨比例，再对所有完整交易日取平均。
        </Typography.Text>
      </> : <Typography.Text type="secondary">完整历史样本不足，暂不发布每日前5 / 前10 / 前15名统计。</Typography.Text>}
    </Card>
    <Row gutter={[16,16]}><Col span={24}><RankingScoreChart records={records}/></Col></Row>
    <Card title="数据新鲜度"><Table size="small" pagination={false} rowKey="key" dataSource={freshness.data ?? []} columns={[{title:'数据项',dataIndex:'label'},{title:'状态',dataIndex:'status',render:(v:string)=><Tag color={v==='ready'?'success':'default'}>{v==='ready'?'已就绪':'缺失'}</Tag>},{title:'更新时间',dataIndex:'updated_at',render:(v:string|null)=>v?new Date(v).toLocaleString():'—'},{title:'大小',dataIndex:'size_bytes',render:(v:number|null)=>v?`${(v/1024).toFixed(1)} KB`:'—'}]}/></Card>
  </Space>
}
