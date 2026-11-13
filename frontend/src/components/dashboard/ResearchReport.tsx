import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Collapse, Select, Space, Statistic, Table, Tabs, Typography, message } from 'antd'
import { backtestApi } from '../../api/backtestApi'
import { MetricGrid } from '../common/MetricGrid'
import { backtestError } from '../../utils/backtest'
import type { BacktestDetail, BacktestRowKind } from '../../types/backtests'

const pct = (value: unknown) => typeof value === 'number' ? `${(value * 100).toFixed(2)}%` : '—'
const number = (v: unknown) => typeof v === 'number' ? v.toLocaleString('zh-CN', { maximumFractionDigits: 4 }) : v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)
const metrics: [string,string,boolean][] = [['total_return','累计收益',true],['annual_return','年化收益',true],['max_drawdown','最大回撤',true],['sharpe','夏普（无风险率0）',false],['sell_win_rate','卖出笔胜率',true],['benchmark_return','参考基准收益',true],['total_fees','总费用（元）',false],['trade_count','交易笔数',false]]
const names: Record<string,string> = { date:'日期',end_date:'兑现日',signal_date:'信号日',code:'股票',side:'方向',price:'成交价',shares:'数量',amount:'金额',fee:'费用',reason:'原因',close:'估值价',cost:'平均成本',market_value:'市值',weight:'目标/持仓权重',stale:'沿用旧价',sellable_shares:'可卖数量',realized_pnl:'已实现盈亏',return:'区间收益',benchmark_return:'参考收益',factor:'因子',ic:'Rank IC',segment:'层数',count:'有效股票数',pairs:'配对数',signal_count:'信号股票数',nav:'净值',lag:'持有交易日',period_count:'有效期数',left:'因子一',right:'因子二',correlation:'秩相关',score:'综合得分',rank:'名次',period:'月份',fees:'费用',trade_count:'交易笔数',mean_exposure:'平均仓位',weights:'因子权重',candidates:'候选股票',next_exposure:'次日目标仓位',regime:'市场状态' }

export function saveBlob(data: Blob, filename: string) {
  const url = URL.createObjectURL(data), a = document.createElement('a')
  a.href = url; a.download = filename; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000)
}

function LineChart({ rows, series }: { rows: Record<string, unknown>[]; series: [string,string][] }) {
  const values = rows.flatMap(r => series.map(([key]) => r[key])).filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  if (rows.length < 2 || !values.length) return <Typography.Text>暂无足够曲线点</Typography.Text>
  const lo = Math.min(...values), hi = Math.max(...values), span = hi-lo || 1
  return <div className="simple-chart"><Typography.Paragraph>{series.map(([,label],i)=>`${i ? '虚线' : '实线'}：${label}`).join('　')}</Typography.Paragraph>
    <svg viewBox="0 0 900 270" role="img" aria-label={series.map(([,label])=>label).join('、')}>
      {series.map(([key],n) => {
        // 缺值处断开曲线，不用0代替数据缺口。
        const path = rows.map((r,i)=> typeof r[key] === 'number' && Number.isFinite(r[key]) ? `${i === 0 || typeof rows[i-1][key] !== 'number' ? 'M' : 'L'}${30+i/(rows.length-1)*840},${230-(Number(r[key])-lo)/span*200}` : '').join(' ')
        return <path key={key} d={path} className={n===0 ? 'chart-line' : undefined} fill="none" stroke="currentColor" strokeDasharray={n ? '6 5' : undefined} opacity={n ? .6 : 1} />
      })}
      <text x="30" y="18" className="chart-label">{hi.toFixed(3)}</text><text x="30" y="255" className="chart-label">{lo.toFixed(3)}</text>
    </svg><div className="chart-range"><span>{String(rows[0].date ?? rows[0].lag ?? '')}</span><span>{String(rows.at(-1)?.date ?? rows.at(-1)?.lag ?? '')}</span></div>
  </div>
}

function Rows({ data, kind, owner }: { data: BacktestDetail; kind: BacktestRowKind; owner: string }) {
  const [page,setPage] = useState(1)
  useEffect(()=>setPage(1),[data.run_id,kind])
  const query=useQuery({queryKey:['backtests',owner,data.run_id,kind,page],queryFn:()=>backtestApi.rows(data.run_id,kind,(page-1)*50)})
  if(query.error) return <Alert type="error" message={backtestError(query.error)} />
  const columns=Object.keys(query.data?.records[0] ?? {}).map(key=>({title:names[key] ?? key,dataIndex:key,
    render:(v:unknown)=>['weight','return','benchmark_return','mean_exposure','next_exposure'].includes(key) ? pct(v) : key==='stale' ? (v?'是':'否') : number(v)}))
  return <Table<Record<string,unknown>> size="small" scroll={{x:true}} loading={query.isLoading} rowKey={(_,i)=>String((page-1)*50+(i??0))}
    dataSource={query.data?.records ?? []} columns={columns} pagination={{current:page,pageSize:50,total:query.data?.total??0,showSizeChanger:false,onChange:setPage}} />
}

function AnalysisCharts({ data, owner }: { data: BacktestDetail; owner: string }) {
  const [factor,setFactor]=useState(data.spec.factors[0]?.field ?? '')
  const ic=useQuery({queryKey:['backtests',owner,data.run_id,'ic-chart',factor],queryFn:()=>backtestApi.rows(data.run_id,'ic_series',0,1500,factor)})
  const long=useQuery({queryKey:['backtests',owner,data.run_id,'ls-chart',factor],queryFn:()=>backtestApi.rows(data.run_id,'long_short',0,1500,factor)})
  const decay=useQuery({queryKey:['backtests',owner,data.run_id,'decay-chart',factor],queryFn:()=>backtestApi.rows(data.run_id,'ic_decay',0,50,factor)})
  const groups=data.factor_metrics?.[factor]
  const means=(groups?.segment_mean_returns ?? []) as (number|null)[]
  const sums=(groups?.segment_cumulative_returns ?? []) as (number|null)[]
  return <Card title="因子研究曲线">
    <Select value={factor} onChange={setFactor} style={{width:180}} options={data.spec.factors.map(f=>({value:f.field}))} />
    <Table size="small" pagination={false} style={{marginTop:16}} rowKey="segment" dataSource={means.map((r,i)=>({segment:i+1,mean:r,cumulative:sums[i]}))} columns={[{title:'层数（1=优先）',dataIndex:'segment'},{title:'平均区间收益',dataIndex:'mean',render:pct},{title:'累计收益',dataIndex:'cumulative',render:pct}]} />
    <Tabs items={[
      {key:'ic',label:'Rank IC',children:ic.error?<Alert type="error" message={backtestError(ic.error)} />:<LineChart rows={ic.data?.records??[]} series={[["ic","Rank IC"]]} />},
      {key:'ls',label:'多空研究净值',children:long.error?<Alert type="error" message={backtestError(long.error)} />:<LineChart rows={long.data?.records??[]} series={[["nav","首尾层收益差净值（非可交易账户）"]]} />},
      {key:'decay',label:'IC衰减',children:decay.error?<Alert type="error" message={backtestError(decay.error)} />:<LineChart rows={decay.data?.records??[]} series={[["ic","按持有交易日的平均IC"]]} />},
    ]} />
  </Card>
}

export function ResearchReport({ data, owner }: { data: BacktestDetail; owner: string }) {
  const equity=useQuery({queryKey:['backtests',owner,data.run_id,'curve'],queryFn:()=>backtestApi.rows(data.run_id,'equity',0,1500),enabled:data.kind==='backtest'})
  const kinds: [BacktestRowKind,string][] = data.kind==='backtest' ? [['trades','全部成交'],['positions','逐日持仓'],['periods','月度汇总'],['signals','信号/动态权重'],['skipped_orders','未成交原因']] : data.kind==='analysis' ? [['ic_series','逐期IC'],['segments','逐期分层'],['long_short','多空研究序列'],['ic_decay','IC衰减'],['correlation','因子相关矩阵']] : [['selections','选股结果'],['signals','权重与择时']]
  const download=async()=>{try{saveBlob(await backtestApi.exportReport(data.run_id),`research-${data.run_id}.json`)}catch(e){message.error(backtestError(e))}}
  return <Space direction="vertical" size="large" style={{width:'100%'}}>
    <Card title={data.spec.name} extra={<Button onClick={()=>void download()}>导出完整JSON报告</Button>}><Typography.Text copyable>{data.run_id}</Typography.Text></Card>
    {data.kind==='backtest' && <><MetricGrid>{metrics.map(([key,label,percent])=><Card key={key}><Statistic title={label} value={data.metrics[key]==null?'—':Number(data.metrics[key])*(percent?100:1)} precision={2} suffix={percent?'%':undefined} /></Card>)}</MetricGrid>
      <Card title="每日净值">{equity.error?<Alert type="error" message={backtestError(equity.error)} />:<LineChart rows={equity.data?.records??[]} series={[["nav","策略"],["benchmark_nav","所选参考基准"]]} />}</Card></>}
    {data.kind==='analysis' && <><Card title="因子指标"><Table pagination={false} rowKey="factor" scroll={{x:true}} dataSource={Object.entries(data.factor_metrics??{}).map(([factor,values])=>({factor,...values}))} columns={[{title:'因子',dataIndex:'factor'},{title:'平均Rank IC',dataIndex:'ic_mean',render:number},{title:'IC IR',dataIndex:'ic_ir',render:number},{title:'正IC比例',dataIndex:'ic_positive_ratio',render:pct},{title:'单调性ρ',dataIndex:'monotonicity_rho',render:number},{title:'单调性r²',dataIndex:'monotonicity_r2',render:number},{title:'多空平均价差',dataIndex:'long_short_spread',render:pct},{title:'有效期数',dataIndex:'period_count'}]} /></Card><AnalysisCharts key={data.run_id} data={data} owner={owner} /></>}
    {data.kind==='screen' && <Card><Typography.Paragraph>选中 {data.metrics.selected_count} 只；有效因子截面 {data.metrics.valid_count} 只。仅生成研究结果，没有下单。</Typography.Paragraph></Card>}
    <Card><Tabs items={kinds.map(([kind,label])=>({key:kind,label,children:<Rows data={data} kind={kind} owner={owner} />}))} /></Card>
    <Collapse items={[{key:'basis',label:'本次参数、数据指纹与模拟边界',children:<>
      <Typography.Paragraph copyable>引擎：{data.engine_version}；数据指纹：{data.data_hash}</Typography.Paragraph>
      <pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify({spec:data.spec,snapshot:data.data_snapshot,diagnostics:data.diagnostics},null,2)}</pre>
      {data.assumptions.map(text=><Typography.Paragraph key={text}>{text}</Typography.Paragraph>)}
    </>}]} />
  </Space>
}
