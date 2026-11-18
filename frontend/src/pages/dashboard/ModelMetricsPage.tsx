import { useQuery } from '@tanstack/react-query'
import { Card, Col, Descriptions, Row, Space } from 'antd'
import { modelApi } from '../../api/modelApi'
import { RecordTable } from '../../components/common/RecordTable'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
import { formatValue } from '../../components/common/ValueText'
export function ModelMetricsPage(){ const metrics=useQuery({queryKey:['web','models','metrics'],queryFn:modelApi.metrics}); const catalog=useQuery({queryKey:['web','models','catalog'],queryFn:modelApi.catalog}); if(metrics.isLoading||catalog.isLoading)return <PageLoading/>; if(metrics.error)return <EmptyState title="模型指标加载失败" description={String(metrics.error)}/>; return <Space direction="vertical" size="large" style={{width:'100%'}}><PageHeader title="模型指标" description="对照现有模型指标、当前默认方案和模型目录。"/><Row gutter={[16,16]}><Col xs={24} xl={12}><Card title="当前模型指标"><Descriptions column={1} size="small">{Object.entries(metrics.data?.metrics??{}).slice(0,30).map(([k,v])=><Descriptions.Item key={k} label={k}>{formatValue(v)}</Descriptions.Item>)}</Descriptions></Card></Col><Col xs={24} xl={12}><Card title="当前默认方案"><Descriptions column={1} size="small">{Object.entries(metrics.data?.selected_strategy??{}).map(([k,v])=><Descriptions.Item key={k} label={k}>{formatValue(v)}</Descriptions.Item>)}</Descriptions></Card></Col></Row><Card title={`模型目录 · ${catalog.data?.total??0} 条`}><RecordTable records={catalog.data?.records??[]} maxColumns={14}/></Card></Space> }
