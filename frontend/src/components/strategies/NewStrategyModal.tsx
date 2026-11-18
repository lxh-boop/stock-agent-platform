import { useEffect, useRef } from 'react'
import { Alert, Button, Col, Form, Input, InputNumber, Modal, Row, Select, Space, Typography, message } from 'antd'
import type { BacktestCatalog } from '../../types/backtests'
import type { NewStrategyDraft } from '../../types/strategies'
import { benchmarkLabels, strategyDraftPrompt } from '../../utils/strategies'

export function NewStrategyModal({ catalog, onClose, onDraft }: {
  catalog?: BacktestCatalog; onClose: () => void; onDraft: (prompt: string) => void
}) {
  const [form] = Form.useForm<NewStrategyDraft>()
  const initialized = useRef(false)
  useEffect(() => {
    if (!initialized.current && catalog?.coverage) {
      initialized.current = true
      form.setFieldsValue({ start_date: catalog.coverage.start_date, end_date: catalog.coverage.end_date })
    }
  }, [catalog, form])

  const prepare = async () => {
    try {
      const values = await form.validateFields()
      if (!catalog) throw new Error('可用因子尚未加载，请稍后重试。')
      onDraft(strategyDraftPrompt(values, catalog))
    } catch (error) {
      if (error instanceof Error) message.error(error.message)
    }
  }

  return <Modal open title="新建策略" width={720} onCancel={onClose} onOk={() => void prepare()}
    okText="去 AI Agent 确认" okButtonProps={{ disabled: !catalog?.coverage }}>
    <Typography.Paragraph type="secondary">填写需求后会打开 AI Agent 并填入草稿。你可以继续修改，点击发送后再确认回测参数。</Typography.Paragraph>
    {!catalog?.coverage && <Alert type="warning" showIcon message={catalog?.message || '本人因子库暂无可用行情，请先完成一次日更。'} />}
    <Form form={form} layout="vertical" initialValues={{ initial_cash: 1000000, max_positions: 10,
      rebalance_days: 5, benchmark: 'pool', factors: [{ direction: -1 }] }}>
      <Form.Item name="name" label="策略名称" rules={[{ required: true, whitespace: true, message: '请填写策略名称' }]}><Input maxLength={80} placeholder="例如：低波动轮动策略" /></Form.Item>
      <Row gutter={16}>
        <Col xs={24} sm={12}><Form.Item name="start_date" label="回测开始日期" rules={[{ required: true }]}><Input type="date" min={catalog?.coverage?.start_date} max={catalog?.coverage?.end_date} /></Form.Item></Col>
        <Col xs={24} sm={12}><Form.Item name="end_date" label="回测结束日期" rules={[{ required: true }]}><Input type="date" min={catalog?.coverage?.start_date} max={catalog?.coverage?.end_date} /></Form.Item></Col>
        <Col xs={24} sm={12}><Form.Item name="initial_cash" label="初始研究资金（元）" rules={[{ required: true }]}><InputNumber min={10000} max={1000000000} step={10000} style={{ width: '100%' }} /></Form.Item></Col>
        <Col xs={24} sm={12}><Form.Item name="max_positions" label="最多持仓数" rules={[{ required: true }]}><InputNumber min={1} max={catalog?.max_positions ?? 50} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
        <Col xs={24} sm={12}><Form.Item name="rebalance_days" label="调仓间隔（交易日）" rules={[{ required: true }]}><InputNumber min={1} max={250} precision={0} style={{ width: '100%' }} /></Form.Item></Col>
        <Col xs={24} sm={12}><Form.Item name="benchmark" label="参考基准" rules={[{ required: true }]}><Select options={Object.entries(benchmarkLabels).map(([value, label]) => ({ value, label }))} /></Form.Item></Col>
      </Row>
      <Form.List name="factors">{(fields, { add, remove }) => <>
        {fields.map(field => <Space key={field.key} wrap align="baseline" className="strategy-factor-input">
          <Form.Item name={[field.name, 'field']} label="因子" rules={[{ required: true, message: '请选择因子' }]}>
            <Select showSearch placeholder="选择本人可用因子" style={{ width: 210 }} options={(catalog?.fields ?? []).map(value => ({ value }))} />
          </Form.Item>
          <Form.Item name={[field.name, 'direction']} label="排序方向"><Select style={{ width: 140 }} options={[{ value: 1, label: '值大优先' }, { value: -1, label: '值小优先' }]} /></Form.Item>
          <Button disabled={fields.length === 1} onClick={() => remove(field.name)}>移除</Button>
        </Space>)}
        <Button disabled={fields.length >= 10} onClick={() => add({ direction: 1 })}>添加因子</Button>
      </>}</Form.List>
    </Form>
    <Typography.Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0 }}>
      股票范围为本人数据库覆盖的股票。这里的初始资金仅用于独立回测，不会改变已有模拟账户的资金和持仓。
    </Typography.Paragraph>
  </Modal>
}
