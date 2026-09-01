import { Card, Col, Row, Table, Tabs, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { RecordTable } from '../common/RecordTable'
import { PaperSectionCard } from './PaperSectionCard'
import type { TablePayload } from '../../types/common'

type RowRecord = Record<string, unknown>

const number = (value: unknown) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
const fixed = (value: unknown, digits = 2) => number(value) === null ? '—' : number(value)!.toFixed(digits)
const percent = (value: unknown) => number(value) === null ? '—' : `${(number(value)! * 100).toFixed(2)}%`
const date = (value: unknown) => String(value ?? '—').slice(0, 19)
const action = (value: unknown) => {
  const normalized = String(value ?? '').toLowerCase()
  if (normalized.includes('buy')) return <Tag color="success">买入</Tag>
  if (normalized.includes('sell') || normalized.includes('reduce')) return <Tag color="error">卖出</Tag>
  return <Tag>持有</Tag>
}

function DataTable({ records, columns, rowKey }: { records: RowRecord[]; columns: ColumnsType<RowRecord>; rowKey: (row: RowRecord, index?: number) => string }) {
  return <Table<RowRecord> size="small" scroll={{ x: 'max-content' }} pagination={{ pageSize: 10, showSizeChanger: true }} dataSource={records} columns={columns} rowKey={rowKey} />
}

const positionColumns: ColumnsType<RowRecord> = [
  { title: '股票', dataIndex: 'stock_code', width: 90 },
  { title: '名称', dataIndex: 'stock_name', width: 100 },
  { title: '数量', dataIndex: 'quantity', width: 80, render: (value) => fixed(value, 0) },
  { title: '成本价', dataIndex: 'cost_price', width: 90, render: (value) => fixed(value) },
  { title: '当前价', dataIndex: 'current_price', width: 90, render: (value) => fixed(value) },
  { title: '持仓市值', dataIndex: 'market_value', width: 110, render: (value) => fixed(value) },
  { title: '仓位', dataIndex: 'position_ratio', width: 85, render: percent },
  { title: '浮动盈亏', dataIndex: 'unrealized_pnl', width: 105, render: (value) => fixed(value) },
  { title: '行业', dataIndex: 'industry', width: 100, render: (value) => String(value || '—') },
  { title: '更新时间', dataIndex: 'updated_at', width: 150, render: date },
]

const orderColumns: ColumnsType<RowRecord> = [
  { title: '交易日', dataIndex: 'trade_date', width: 105, render: date },
  { title: '股票', dataIndex: 'stock_code', width: 90 },
  { title: '名称', dataIndex: 'stock_name', width: 100 },
  { title: '动作', dataIndex: 'action', width: 75, render: action },
  { title: '成交价', dataIndex: 'executed_price', width: 90, render: (value) => fixed(value) },
  { title: '数量', dataIndex: 'quantity', width: 80, render: (value) => fixed(value, 0) },
  { title: '成交金额', dataIndex: 'order_amount', width: 110, render: (value) => fixed(value) },
  { title: '费用', dataIndex: 'total_fee', width: 85, render: (value) => fixed(value) },
  { title: '目标仓位', dataIndex: 'target_weight', width: 95, render: percent },
  { title: '原因', dataIndex: 'reason', width: 320, ellipsis: true },
]

const decisionColumns: ColumnsType<RowRecord> = [
  { title: '交易日', dataIndex: 'trade_date', width: 105, render: date },
  { title: '排名', dataIndex: 'original_rank', width: 65 },
  { title: '股票', dataIndex: 'stock_code', width: 90 },
  { title: '名称', dataIndex: 'stock_name', width: 100 },
  { title: '动作', dataIndex: 'action', width: 75, render: action },
  { title: '模型分', dataIndex: 'original_score', width: 85, render: percent },
  { title: '最终分', dataIndex: 'final_score', width: 85, render: percent },
  { title: '目标仓位', dataIndex: 'target_weight', width: 95, render: percent },
  { title: '参考价', dataIndex: 'current_price', width: 85, render: (value) => fixed(value) },
  { title: '风险', dataIndex: 'risk_level', width: 75 },
  { title: '原因', dataIndex: 'reason', width: 320, ellipsis: true },
]

const cashFlowColumns: ColumnsType<RowRecord> = [
  { title: '生效日', dataIndex: 'effective_date', width: 105, render: date },
  { title: '类型', dataIndex: 'flow_type', width: 85 },
  { title: '金额', dataIndex: 'amount', width: 110, render: (value) => fixed(value) },
  { title: '状态', dataIndex: 'status', width: 90 },
  { title: '原因', dataIndex: 'reason', width: 240, ellipsis: true },
  { title: '创建时间', dataIndex: 'created_at', width: 150, render: date },
]

export function PaperTables({ positions, orders, decisions, cashFlows }: {
  positions: TablePayload<Record<string, unknown>>
  orders: TablePayload<Record<string, unknown>>
  decisions: TablePayload<Record<string, unknown>>
  cashFlows: TablePayload<Record<string, unknown>>
}) {
  return <PaperSectionCard sectionKey="paper-records" title="持仓、订单与决策记录"><Tabs items={[
    { key: 'positions', label: `当前持仓 (${positions.total})`, children: <DataTable records={positions.records} columns={positionColumns} rowKey={(row) => String(row.position_id ?? row.stock_code)} /> },
    { key: 'orders', label: `订单历史 (${orders.total})`, children: <DataTable records={orders.records} columns={orderColumns} rowKey={(row, index) => String(row.order_id ?? index)} /> },
    { key: 'decisions', label: `当日决策 (${decisions.total})`, children: <DataTable records={decisions.records} columns={decisionColumns} rowKey={(row, index) => String(row.decision_id ?? `${row.stock_code}-${index}`)} /> },
    { key: 'cash', label: `资金流水 (${cashFlows.total})`, children: <DataTable records={cashFlows.records} columns={cashFlowColumns} rowKey={(row, index) => String(row.cash_flow_id ?? index)} /> },
  ]} /></PaperSectionCard>
}

export function RiskAndDiagnostics({ risk, diagnostics, settings }: { risk: Record<string, unknown>; diagnostics: Record<string, unknown>; settings: Record<string, unknown> }) {
  return <PaperSectionCard sectionKey="risk-diagnostics" title="风险、执行诊断与交易设置">
    <Row gutter={[16, 16]}>
      <Col xs={24} xl={8}><Card size="small" title="组合风险"><RecordTable records={[risk]} maxColumns={16} /></Card></Col>
      <Col xs={24} xl={8}><Card size="small" title="执行诊断"><RecordTable records={[diagnostics]} maxColumns={16} /></Card></Col>
      <Col xs={24} xl={8}><Card size="small" title="交易设置"><RecordTable records={[settings]} maxColumns={16} /></Card></Col>
    </Row>
  </PaperSectionCard>
}
