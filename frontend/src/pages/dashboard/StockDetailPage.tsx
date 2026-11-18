import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Col, Descriptions, Row, Space, Statistic } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import { dashboardApi } from '../../api/dashboardApi'
import { stockApi } from '../../api/stockApi'
import { EventFactorTable } from '../../components/dashboard/EventFactorTable'
import { EvidenceSearchPanel } from '../../components/dashboard/EvidenceSearchPanel'
import { ExplanationPanel } from '../../components/dashboard/ExplanationPanel'
import { StockPriceChart } from '../../components/dashboard/StockPriceChart'
import { StockSelector } from '../../components/dashboard/StockSelector'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { formatValue } from '../../components/common/ValueText'

export function StockDetailPage() {
  const params=useParams(); const navigate=useNavigate(); const [code,setCode]=useState(params.stockCode ?? '')
  useEffect(()=>setCode(params.stockCode ?? ''),[params.stockCode])
  const rankings=useQuery({queryKey:['web','dashboard','rankings','selector'],queryFn:()=>dashboardApi.rankings(0,500)})
  useEffect(()=>{ if(!code && rankings.data?.records[0]?.code){ const first=String(rankings.data.records[0].code); setCode(first); navigate(`/stocks/${first}`,{replace:true})}},[code,navigate,rankings.data])
  const detail=useQuery({queryKey:['web','stock',code,'detail'],queryFn:()=>stockApi.detail(code),enabled:Boolean(code)})
  const history=useQuery({queryKey:['web','stock',code,'history'],queryFn:()=>stockApi.history(code,180),enabled:Boolean(code)})
  const explanation=useQuery({queryKey:['web','stock',code,'explanation'],queryFn:()=>stockApi.explanation(code),enabled:Boolean(code)})
  const [evidenceQuery,setEvidenceQuery]=useState(''); const evidence=useQuery({queryKey:['web','stock',code,'evidence',evidenceQuery],queryFn:()=>stockApi.evidence(code,evidenceQuery,12),enabled:Boolean(code && evidenceQuery)})
  if (rankings.isLoading || (code && detail.isLoading)) return <PageLoading/>
  if (!code) return <EmptyState title="暂无股票" description="排名数据中没有可选择的股票。"/>
  return <Space direction="vertical" size="large" style={{width:'100%'}}><PageHeader title="个股详情" description="读取排名、行情、证据和已有解释缓存，不生成新解释。"/><StockSelector records={rankings.data?.records??[]} value={code} onChange={(next)=>navigate(`/stocks/${next}`)}/>
  <Row gutter={[16,16]}><Col xs={24} lg={8}><Card><Statistic title="股票" value={`${detail.data?.stock_code ?? code} ${detail.data?.name ?? ''}`}/><Statistic title="本地事件数" value={detail.data?.event_count ?? 0}/></Card></Col><Col xs={24} lg={16}><Card title="当前快照"><Descriptions size="small" column={{xs:1,sm:2,md:3}}>{Object.entries({...detail.data?.ranking,...detail.data?.market}).slice(0,18).map(([k,v])=><Descriptions.Item key={k} label={k}>{formatValue(v)}</Descriptions.Item>)}</Descriptions></Card></Col></Row>
  <StockPriceChart records={history.data?.records??[]}/><EvidenceSearchPanel loading={evidence.isFetching} onSearch={setEvidenceQuery}/>{evidenceQuery&&<EventFactorTable records={evidence.data?.records??[]}/>}<ExplanationPanel explanation={explanation.data}/></Space>
}
