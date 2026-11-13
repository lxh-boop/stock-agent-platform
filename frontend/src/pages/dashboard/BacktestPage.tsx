import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Form, Input, Modal, Space, Table, Tabs, Typography, message } from 'antd'
import { backtestApi } from '../../api/backtestApi'
import { cancelTask, listTasks } from '../../api/taskApi'
import { PageHeader } from '../../components/common/PageHeader'
import { TaskProgress } from '../../components/tasks/TaskProgress'
import { ResearchForm, studyLabels } from '../../components/dashboard/ResearchForm'
import { ResearchReport } from '../../components/dashboard/ResearchReport'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import { useTaskStore } from '../../stores/taskStore'
import { TERMINAL_TASK_STATUSES } from '../../types/task'
import type { BacktestSpec, AnalysisSpec, SavedStrategy, StudyKind } from '../../types/backtests'
import { backtestError, backtestRequestId, fromStudySpec, toStudySpec, toBacktestSpec, type BacktestForm } from '../../utils/backtest'

const pct = (v: unknown) => typeof v === 'number' ? `${(v*100).toFixed(2)}%` : '—'

export function BacktestPage() {
  const owner=useCurrentUserId(), client=useQueryClient()
  const [form]=Form.useForm<BacktestForm>()
  const [kind,setKind]=useState<StudyKind>('backtest')
  const [selected,setSelected]=useState(''), [strategyId,setStrategyId]=useState('')
  const [historyPage,setHistoryPage]=useState(1), [strategyPage,setStrategyPage]=useState(1)
  const [jsonEditor,setJsonEditor]=useState<string|null>(null)
  const initialized=useRef(''), lastFinished=useRef('')
  const pending=useRef<{fingerprint:string;id:string}|null>(null)
  const setTask=useTaskStore(s=>s.setTask), clearTask=useTaskStore(s=>s.clear)
  const catalog=useQuery({queryKey:['backtests',owner,'catalog'],queryFn:backtestApi.catalog})
  const history=useQuery({queryKey:['backtests',owner,'history',historyPage],queryFn:()=>backtestApi.history((historyPage-1)*30)})
  const strategies=useQuery({queryKey:['backtests',owner,'strategies',strategyPage],queryFn:()=>backtestApi.strategies((strategyPage-1)*30)})
  const tasks=useQuery({queryKey:['backtests',owner,'tasks'],queryFn:()=>listTasks({task_type:'backtest.run',owner_id:owner,limit:5}),refetchInterval:2000})
  const latest=tasks.data?.[0], active=tasks.data?.find(t=>!TERMINAL_TASK_STATUSES.has(t.status))
  const detail=useQuery({queryKey:['backtests',owner,selected,'detail'],queryFn:()=>backtestApi.detail(selected),enabled:Boolean(selected)})
  useEffect(()=>{ setSelected('');setStrategyId('');setHistoryPage(1);setStrategyPage(1);initialized.current='';pending.current=null;lastFinished.current='';form.resetFields() },[owner,form])
  useEffect(()=>{
    if(catalog.data?.coverage && initialized.current!==owner){
      form.setFieldsValue({start_date:catalog.data.coverage.start_date,end_date:catalog.data.coverage.end_date})
      initialized.current=owner
    }
  },[catalog.data,form,owner])
  useEffect(()=>{if(!selected && history.data?.records.length)setSelected(history.data.records[0].run_id)},[history.data,selected])
  useEffect(()=>{
    if(latest?.status==='succeeded' && lastFinished.current!==latest.task_id){
      lastFinished.current=latest.task_id
      void client.invalidateQueries({queryKey:['backtests',owner,'history']})
      const result=latest.result as {run_id?:string}|undefined
      if(result?.run_id)setSelected(result.run_id)
    }
  },[latest,client,owner])
  const submit=useMutation({
    mutationFn:async({spec,mode}:{spec:BacktestSpec|AnalysisSpec;mode:StudyKind})=>{
      const fingerprint=JSON.stringify([mode,spec])
      if(!pending.current || pending.current.fingerprint!==fingerprint)pending.current={fingerprint,id:backtestRequestId()}
      return backtestApi.run(spec,pending.current.id,mode)
    },
    onSuccess:task=>{pending.current=null;clearTask();setTask(task);void tasks.refetch();message.success('已提交本人研究任务')},
    onError:error=>message.error(backtestError(error)),
  })
  const cancel=useMutation({mutationFn:cancelTask,onSuccess:()=>{void tasks.refetch()},onError:error=>message.error(backtestError(error))})
  const save=useMutation({mutationFn:({id,spec}:{id:string;spec:BacktestSpec})=>backtestApi.saveStrategy(id,spec),
    onSuccess:s=>{setStrategyId(s.strategy_id);void strategies.refetch();message.success('已保存个人策略')},onError:error=>message.error(backtestError(error))})
  const launch=async()=>{
    await form.validateFields()
    // 折叠面板尚未挂载的参数也必须保留，校验结果只含已注册字段。
    const spec=toStudySpec(form.getFieldsValue(true),kind)
    Modal.confirm({title:`运行${studyLabels[kind]}？`,content:`${spec.start_date} 至 ${spec.end_date}，${spec.factors.length} 个因子。只保存个人研究报告，不修改模拟盘；选择指数时可能使用本人数据源配额。`,onOk:()=>submit.mutateAsync({spec,mode:kind}).then(()=>undefined)})
  }
  const saveConfig=async(asNew:boolean)=>{
    await form.validateFields()
    const spec=toBacktestSpec(form.getFieldsValue(true))
    Modal.confirm({title:asNew?'保存为新策略？':'保存策略修改？',content:'仅保存参数，不自动交易或运行日更。',onOk:()=>save.mutateAsync({id:asNew||!strategyId?backtestRequestId():strategyId,spec}).then(()=>undefined)})
  }
  const edit=(s:SavedStrategy)=>{setKind('backtest');setStrategyId(s.strategy_id);form.resetFields();form.setFieldsValue(fromStudySpec(s.spec));window.scrollTo({top:0,behavior:'smooth'})}
  const remove=(id:string,strategy:boolean)=>Modal.confirm({title:strategy?'删除此个人策略？':'删除此个人报告？',content:'不能撤销；不会删除因子数据或其它报告。',okButtonProps:{danger:true},onOk:async()=>{
    try{if(strategy){await backtestApi.deleteStrategy(id);void strategies.refetch();if(strategyId===id)setStrategyId('')}
      else{await backtestApi.deleteReport(id);void history.refetch();if(selected===id)setSelected('')}
    }catch(e){message.error(backtestError(e));throw e}
  }})
  const openJson=()=>{try{setJsonEditor(JSON.stringify(toStudySpec(form.getFieldsValue(true),kind),null,2))}catch(e){message.error(backtestError(e))}}
  const applyJson=()=>{try{
    const spec=JSON.parse(jsonEditor??'') as BacktestSpec|AnalysisSpec
    if(!spec || typeof spec!=='object' || !Array.isArray(spec.factors))throw new Error('请输入包含factors的策略规格对象')
    form.resetFields();form.setFieldsValue(fromStudySpec(spec));setStrategyId('');setJsonEditor(null);message.success('已填入表单，运行前服务端会严格校验')
  }catch(e){message.error(backtestError(e))}}
  return <Space direction="vertical" size="large" style={{width:'100%'}}>
    <PageHeader title="策略研究" description="个人Alpha158 → 策略回测 / 因子分析 / 每日选股。页面与Agent使用同一服务。" />
    <Alert type="warning" showIcon message="研究模拟，不是实盘撮合" description="支持次日执行、动态权重、仓位函数与指数择时。固定涨跌幅和公司行动均为近似；不模拟冲击、退市清算，不冒充历史指数成分。缺数据报错，不生成演示收益。" />
    {catalog.error?<Alert type="error" message={backtestError(catalog.error)} />:!catalog.data?.coverage?<Alert type="info" message={catalog.data?.message??'正在读取个人因子库…'} />:<Typography.Text>可用数据：{catalog.data.coverage.start_date} ～ {catalog.data.coverage.end_date}，{catalog.data.coverage.stock_count} 只股票。</Typography.Text>}
    <Card title={strategyId?'编辑个人策略':'新建研究'} extra={<Button onClick={openJson}>导入 / 查看 JSON 参数</Button>}>
      <Tabs activeKey={kind} onChange={value=>setKind(value as StudyKind)} items={(Object.keys(studyLabels) as StudyKind[]).map(key=>({key,label:studyLabels[key]}))} />
      <ResearchForm form={form} kind={kind} catalog={catalog.data} />
      <Space wrap style={{marginTop:20}}>
        <Button type="primary" aria-label={`运行${studyLabels[kind]}`} data-testid={`run-research-${kind}`} disabled={!catalog.data?.coverage||Boolean(active)} loading={submit.isPending} onClick={()=>void launch().catch(e=>{if(e instanceof Error)message.error(e.message)})}>{`运行${studyLabels[kind]}`}</Button>
        {kind==='backtest' && <><Button loading={save.isPending} onClick={()=>void saveConfig(true).catch(()=>undefined)}>另存为新策略</Button>{strategyId&&<Button loading={save.isPending} onClick={()=>void saveConfig(false).catch(()=>undefined)}>保存修改</Button>}</>}
      </Space>
    </Card>
    <Modal title={`导入 / 查看${studyLabels[kind]}规格（无旧格式兼容）`} open={jsonEditor!==null} onCancel={()=>setJsonEditor(null)} onOk={applyJson} okText="应用到表单" width={760}><Input.TextArea rows={18} value={jsonEditor??''} onChange={e=>setJsonEditor(e.target.value)} /></Modal>
    {latest&&<Card title="最近研究任务"><TaskProgress task={active??latest} />{active&&<Button danger loading={cancel.isPending} onClick={()=>cancel.mutate(active.task_id)}>取消本人任务</Button>}</Card>}
    <Card title="个人策略库">
      {strategies.error&&<Alert type="error" message={backtestError(strategies.error)} />}
      <Table size="small" loading={strategies.isLoading} rowKey="strategy_id" dataSource={strategies.data?.records??[]} columns={[
        {title:'名称',render:(_,s)=><Button type="link" onClick={()=>edit(s)}>{s.spec.name}</Button>},
        {title:'规则',render:(_,s)=>`${s.spec.factors.length} 因子 / ${s.spec.rebalance_days===0?'条件触发':`每${s.spec.rebalance_days}日`}`},
        {title:'修改时间',dataIndex:'updated_at',render:v=>String(v).slice(0,19)},
        {title:'操作',render:(_,s)=><Space><Button onClick={()=>edit(s)}>编辑 / 重跑</Button><Button danger onClick={()=>remove(s.strategy_id,true)}>删除</Button></Space>},
      ]} pagination={{current:strategyPage,total:strategies.data?.total??0,pageSize:30,showSizeChanger:false,onChange:setStrategyPage}} />
    </Card>
    <Card title="历史报告">
      {history.error&&<Alert type="error" message={backtestError(history.error)} />}
      <Table size="small" loading={history.isLoading} rowKey="run_id" dataSource={history.data?.records??[]} columns={[
        {title:'名称',render:(_,r)=><Button type="link" onClick={()=>setSelected(r.run_id)}>{r.spec.name}</Button>},
        {title:'类型',dataIndex:'kind',render:(v:StudyKind)=>studyLabels[v]},
        {title:'区间',render:(_,r)=>`${r.spec.start_date} ～ ${r.spec.end_date}`},
        {title:'收益 / 有效期数 / 选中数',render:(_,r)=>r.kind==='backtest'?pct(r.metrics.total_return):String(r.metrics.period_count??r.metrics.selected_count??'—')},
        {title:'保存时间',dataIndex:'created_at',render:v=>String(v).slice(0,19)},
        {title:'操作',render:(_,r)=><Space><Button onClick={()=>{setKind(r.kind);setStrategyId('');form.resetFields();form.setFieldsValue(fromStudySpec(r.spec));window.scrollTo({top:0,behavior:'smooth'})}}>复用参数</Button><Button danger onClick={()=>remove(r.run_id,false)}>删除</Button></Space>},
      ]} pagination={{current:historyPage,total:history.data?.total??0,pageSize:30,showSizeChanger:false,onChange:setHistoryPage}} />
    </Card>
    {detail.error&&<Alert type="error" message={backtestError(detail.error)} />}
    {detail.data&&<ResearchReport key={selected} data={detail.data} owner={owner} />}
    <Typography.Text type="secondary">本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。</Typography.Text>
  </Space>
}
