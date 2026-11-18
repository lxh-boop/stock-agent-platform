import { useQuery } from '@tanstack/react-query'
import { Card, Collapse, Space, Typography } from 'antd'
import { modelApi } from '../../api/modelApi'
import { RecordTable } from '../../components/common/RecordTable'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
export function ModelSearchPage(){ const query=useQuery({queryKey:['web','models','search-results'],queryFn:modelApi.searchResults}); if(query.isLoading)return <PageLoading/>; if(query.error)return <EmptyState title="模型搜索结果加载失败" description={String(query.error)}/>; const d=query.data; return <Space direction="vertical" size="large" style={{width:'100%'}}><PageHeader title="模型搜索与回测" description="只查看已产生的候选、统一回测和目标搜索结果；不启动搜索、不保存默认方案。"/><Card title="候选模型"><RecordTable records={d?.candidates.records??[]} maxColumns={15}/></Card><Card title="统一回测汇总"><RecordTable records={d?.master_backtests.records??[]} maxColumns={16}/></Card><Card title="目标搜索结果"><RecordTable records={d?.target_results.records??[]} maxColumns={16}/></Card>{Boolean(d?.errors.records.length)&&<Card title="错误记录"><RecordTable records={d?.errors.records??[]}/></Card>}<Collapse items={[{key:'report',label:'模型发现报告',children:<Typography.Paragraph style={{whiteSpace:'pre-wrap'}}>{d?.discovery_report||'暂无报告'}</Typography.Paragraph>}]} /></Space> }
