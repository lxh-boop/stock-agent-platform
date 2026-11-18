import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Col, Collapse, Empty, Form, Input, Modal, Row, Segmented, Space, Table, Tag, Typography, message } from 'antd'
import { factorApi } from '../../api/factorApi'
import { errorMessage } from '../../api/errors'
import { createWriteMeta } from '../../api/writeOperationApi'
import { PageHeader } from '../../components/common/PageHeader'
import { useCurrentUserId } from '../../hooks/useCurrentUserId'
import type { FactorCatalog, FactorDefinition, FactorDraft, FactorValidation, FactorValue, FactorValues } from '../../types/factors'
import './factors.css'

function ValuesTable({ data, loading = false }: { data?: FactorValues; loading?: boolean }) {
  return <Space orientation="vertical" style={{ width: '100%' }}>
    {data?.message && <Typography.Text type="secondary">{data.message}</Typography.Text>}
    <Table<FactorValue>
      size="small" loading={loading} dataSource={data?.items ?? []}
      rowKey={row => `${row.code}-${row.date}`} pagination={false}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={data?.status === 'insufficient_history' ? '历史行情不足，暂时无法计算' : '暂无因子数值'} /> }}
      columns={[
        { title: '股票代码', dataIndex: 'code' },
        { title: '交易日期', dataIndex: 'date' },
        { title: '因子值', dataIndex: 'value', align: 'right', render: (value: number | null) => value === null ? '—' : Number(value.toPrecision(8)).toString() },
      ]}
    />
  </Space>
}

function FactorDetail({ factor, owner }: { factor: FactorDefinition; owner: string }) {
  const [codeInput, setCodeInput] = useState('')
  const [code, setCode] = useState('')
  const [filterError, setFilterError] = useState('')
  const values = useQuery({ queryKey: ['factors', owner, 'values', factor.factor_id, code], queryFn: () => factorApi.values(factor.factor_id, code) })
  const filter = (value: string) => {
    const next = value.trim()
    if (next && !/^\d{6}$/.test(next)) { setFilterError('请输入六位股票代码'); return }
    setFilterError(''); setCode(next)
  }
  return <Card title={factor.name} extra={<Tag>{factor.source === 'alpha158' ? 'Alpha158' : '个人因子'}</Tag>}>
    <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
      {factor.description && <Typography.Paragraph style={{ marginBottom: 0 }}>{factor.description}</Typography.Paragraph>}
      <Typography.Text strong>Python 表达式</Typography.Text>
      <pre className="factor-expression">{factor.expression}</pre>
      <div className="factor-toolbar">
        <Typography.Text strong>{factor.source === 'alpha158' ? '数据库最近数值' : '最近行情计算预览'}</Typography.Text>
        <Button size="small" aria-label="刷新数值" loading={values.isFetching} disabled={values.isFetching} onClick={() => void values.refetch()}>刷新数值</Button>
      </div>
      <Input.Search aria-label="筛选股票代码" placeholder="六位股票代码（可选）" allowClear value={codeInput} onChange={event => {
        setCodeInput(event.target.value)
        if (!event.target.value) filter('')
      }} onSearch={filter} enterButton={<Button aria-label="查询">查询</Button>} />
      {filterError && <Typography.Text type="danger" role="alert">{filterError}</Typography.Text>}
      {values.error ? <Alert type="error" showIcon title="因子数值读取失败" description={errorMessage(values.error)} action={<Button aria-label="重试" onClick={() => void values.refetch()}>重试</Button>} /> : <>
        {values.data?.as_of && <Typography.Text type="secondary">最新交易日：{values.data.as_of}</Typography.Text>}
        <ValuesTable data={values.data} loading={values.isLoading} />
      </>}
    </Space>
  </Card>
}

function AddFactorModal({ catalog, onCancel, onCreated }: { catalog: FactorCatalog; onCancel: () => void; onCreated: (factor: FactorDefinition) => void }) {
  const [form] = Form.useForm<FactorDraft>()
  const [validation, setValidation] = useState<FactorValidation | null>(null)
  const [failure, setFailure] = useState('')
  const writeMeta = useRef<ReturnType<typeof createWriteMeta> | null>(null)
  const validate = useMutation({ mutationFn: factorApi.validate })
  const save = useMutation({ mutationFn: factorApi.create })
  const pending = validate.isPending || save.isPending
  const resetValidation = () => { setValidation(null); setFailure(''); writeMeta.current = null }
  const check = async () => {
    try {
      const values = await form.validateFields()
      setFailure('')
      const checked = await validate.mutateAsync({ name: values.name.trim(), expression: values.expression.trim() })
      setValidation(checked)
      writeMeta.current = createWriteMeta('factor')
    } catch (error) {
      if (error && typeof error === 'object' && 'errorFields' in error) return
      setFailure(errorMessage(error))
    }
  }
  const submit = async () => {
    if (!validation || !writeMeta.current) return
    setFailure('')
    try {
      const result = await save.mutateAsync({ name: validation.name, expression: validation.expression, ...writeMeta.current, confirmed: true })
      message.success(result.replayed ? '因子已保存' : '因子已添加')
      onCreated(result.factor)
    } catch (error) { setFailure(errorMessage(error)) }
  }
  return <Modal open title={validation ? '确认添加因子' : '添加因子'} width={780} onCancel={onCancel} closable={!pending} maskClosable={!pending} keyboard={!pending}
    footer={<Space><Button onClick={onCancel} disabled={pending}>取消</Button>{validation && <Button disabled={pending} onClick={resetValidation}>返回修改</Button>}<Button type="primary" loading={pending} disabled={pending} aria-busy={pending} aria-label={validation ? '确认添加' : '校验并继续'} onClick={() => void (validation ? submit() : check())}>{validation ? '确认添加' : '校验并继续'}</Button></Space>}>
    <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
      {failure && <Alert type="error" showIcon title={failure} />}
      <Form form={form} layout="vertical" disabled={pending || Boolean(validation)} initialValues={{ name: '', expression: catalog.templates[0]?.expression ?? '' }} onValuesChange={resetValidation}>
        <Form.Item name="name" label="因子名称" rules={[{ required: true, whitespace: true, message: '请填写因子名称' }]}>
          <Input maxLength={80} placeholder="例如：五日动量" autoFocus />
        </Form.Item>
        <Form.Item name="expression" label="Python 表达式" rules={[{ required: true, whitespace: true, message: '请填写表达式' }, { max: catalog.syntax.max_length, message: `表达式不能超过 ${catalog.syntax.max_length} 个字符` }]}>
          <Input.TextArea className="factor-expression-input" rows={4} maxLength={catalog.syntax.max_length} placeholder="close / delay(close, 5) - 1" spellCheck={false} />
        </Form.Item>
      </Form>
      {validation ? <>
        <Typography.Text>需要回看 {validation.lookback_rows} 个交易日，使用字段：{validation.fields.join('、')}。</Typography.Text>
        <Typography.Text type="secondary">将保存到你的个人因子库。以下为最近行情的计算预览。</Typography.Text>
        <ValuesTable data={validation.preview} />
      </> : <>
        <Typography.Text type="secondary">按模板填写一条表达式，无需写函数或 import。每只股票独立计算，窗口按交易日计。</Typography.Text>
        <div className="factor-template-grid">{catalog.templates.map(template => <div className="factor-template" key={template.name}>
          <div className="factor-toolbar"><Typography.Text strong>{template.name}</Typography.Text><Button size="small" disabled={pending} onClick={() => { form.setFieldValue('expression', template.expression); resetValidation() }}>使用模板</Button></div>
          <pre className="factor-expression">{template.expression}</pre>
          <Typography.Text type="secondary">{template.description}</Typography.Text>
        </div>)}</div>
        <Collapse items={[{ key: 'syntax', label: '可用字段与函数', children: <Space orientation="vertical" style={{ width: '100%' }}>
          <Typography.Text>字段：{catalog.syntax.fields.join('、')}。最大窗口：{catalog.syntax.max_window} 个交易日。</Typography.Text>
          {catalog.syntax.functions.map(fn => <div key={fn.signature}><Typography.Text code>{fn.signature}</Typography.Text><Typography.Text> {fn.description}</Typography.Text></div>)}
        </Space> }]} />
      </>}
    </Space>
  </Modal>
}

function FactorLibrary({ owner }: { owner: string }) {
  const client = useQueryClient()
  const [search, setSearch] = useState('')
  const [source, setSource] = useState('all')
  const [selectedId, setSelectedId] = useState('')
  const [adding, setAdding] = useState(false)
  const [page, setPage] = useState(1)
  const catalog = useQuery({ queryKey: ['factors', owner, 'catalog'], queryFn: factorApi.catalog })
  const items = useMemo(() => (catalog.data?.items ?? []).filter(item => (source === 'all' || item.source === source) && `${item.name} ${item.expression} ${item.description}`.toLowerCase().includes(search.trim().toLowerCase())), [catalog.data, source, search])
  const selected = catalog.data?.items.find(item => item.factor_id === selectedId) ?? items[0]
  const created = (factor: FactorDefinition) => {
    client.setQueryData<FactorCatalog>(['factors', owner, 'catalog'], current => current ? {
      ...current, items: [...current.items.filter(item => item.factor_id !== factor.factor_id), factor],
    } : current)
    setSelectedId(factor.factor_id); setSearch(''); setSource('custom'); setPage(1); setAdding(false)
    void client.invalidateQueries({ queryKey: ['factors', owner, 'catalog'] })
  }
  return <Space orientation="vertical" size="large" style={{ width: '100%' }}>
    <div className="factor-toolbar">
      <PageHeader title="因子库" description="查看数据库中的 Alpha158 和个人因子，浏览最近因子数值。" />
      <Button type="primary" disabled={!catalog.data} onClick={() => setAdding(true)}>添加因子</Button>
    </div>
    {catalog.error && <Alert type="error" showIcon title="因子库加载失败" description={errorMessage(catalog.error)} action={<Button aria-label="重试" onClick={() => void catalog.refetch()}>重试</Button>} />}
    <Row gutter={[20, 20]} style={{ width: '100%', marginInline: 0 }}>
      <Col xs={24} xl={14} style={{ paddingLeft: 0 }}><Card title="数据库因子" extra={<Button loading={catalog.isFetching} onClick={() => void catalog.refetch()}>刷新</Button>}>
        <Space orientation="vertical" size="middle" style={{ width: '100%' }}>
          {catalog.data && <Typography.Text type="secondary">共 {catalog.data.total} 个因子 · Alpha158 {catalog.data.system_count} 个 · 个人因子 {catalog.data.custom_count} 个</Typography.Text>}
          <Input.Search aria-label="搜索因子" placeholder="搜索名称、表达式或说明" allowClear value={search} onChange={event => { setSearch(event.target.value); setPage(1) }} />
          <Segmented value={source} options={[{ label: '全部', value: 'all' }, { label: 'Alpha158', value: 'alpha158' }, { label: '个人因子', value: 'custom' }]} onChange={value => { setSource(value); setPage(1); setSelectedId('') }} />
          <Table<FactorDefinition> size="small" rowKey="factor_id" loading={catalog.isLoading} dataSource={items}
            rowClassName={item => item.factor_id === selected?.factor_id ? 'factor-row-selected' : ''}
            locale={{ emptyText: '没有匹配的因子' }}
            columns={[
              { title: '名称', dataIndex: 'name', width: '32%', render: (_, factor) => <Button type="link" className="factor-name" onClick={() => setSelectedId(factor.factor_id)}>{factor.name}</Button> },
              { title: '来源', dataIndex: 'source', width: 100, render: value => value === 'alpha158' ? 'Alpha158' : '个人因子' },
              { title: '说明', dataIndex: 'description', ellipsis: true, render: (value: string, factor) => value || factor.expression },
            ]}
            pagination={{ current: page, total: items.length, pageSize: 15, showSizeChanger: false, onChange: setPage, showTotal: total => `${total} 个因子` }}
          />
        </Space>
      </Card></Col>
      <Col xs={24} xl={10} style={{ paddingRight: 0 }}>{selected ? <FactorDetail key={`${owner}-${selected.factor_id}`} factor={selected} owner={owner} /> : <Card><Empty description="选择一个因子查看表达式和数值" /></Card>}</Col>
    </Row>
    {adding && catalog.data && <AddFactorModal catalog={catalog.data} onCancel={() => setAdding(false)} onCreated={created} />}
  </Space>
}

export function FactorLibraryPage() {
  const owner = useCurrentUserId()
  return <FactorLibrary key={owner} owner={owner} />
}
