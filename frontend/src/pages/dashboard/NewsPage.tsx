import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Input, Select, Space } from 'antd'
import { newsApi } from '../../api/newsApi'
import { NewsEventTable } from '../../components/dashboard/NewsEventTable'
import { EmptyState } from '../../components/common/EmptyState'
import { PageHeader } from '../../components/common/PageHeader'
import { PageLoading } from '../../components/common/PageLoading'
export function NewsPage(){ const [stock,setStock]=useState(''); const [eventType,setEventType]=useState('all'); const query=useQuery({queryKey:['web','news',stock,eventType],queryFn:()=>newsApi.events({stock_code:stock||undefined,event_type:eventType,limit:300})}); if(query.isLoading)return <PageLoading/>; if(query.error)return <EmptyState title="新闻事件加载失败" description={String(query.error)}/>; return <Space direction="vertical" size="large" style={{width:'100%'}}><PageHeader title="新闻事件" description="展示本地缓存中的新闻和公告，不联网下载。"/><Card><Space wrap><Input allowClear value={stock} onChange={(e)=>setStock(e.target.value.replace(/\D/g,'').slice(0,6))} placeholder="股票代码" style={{width:180}}/><Select value={eventType} onChange={setEventType} style={{width:160}} options={[{value:'all',label:'全部事件'},{value:'positive',label:'正面'},{value:'negative',label:'负面'},{value:'risk',label:'风险'},{value:'neutral',label:'中立'}]}/></Space></Card><Card title={`事件列表 · ${query.data?.total??0} 条`}><NewsEventTable records={query.data?.records??[]}/></Card></Space> }
