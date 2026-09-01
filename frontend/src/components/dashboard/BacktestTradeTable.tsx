import { Card, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'

type Row = Record<string, unknown>

function number(value: unknown): number | null {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function fixed(value: unknown, digits = 2): string {
  const parsed = number(value)
  return parsed === null ? '—' : parsed.toFixed(digits)
}

function percent(value: unknown): string {
  const parsed = number(value)
  return parsed === null ? '—' : `${(parsed * 100).toFixed(2)}%`
}

export function BacktestTradeTable({ records }: { records: Row[] }) {
  const columns: ColumnsType<Row> = [
    { title: '信号日', dataIndex: 'date', width: 110, render: (value) => String(value ?? '').slice(0, 10) },
    { title: '目标日', dataIndex: 'prediction_date', width: 110, render: (value) => String(value ?? '').slice(0, 10) },
    { title: '排名', dataIndex: 'rank', width: 65 },
    { title: '股票', dataIndex: 'code', width: 90 },
    { title: '名称', dataIndex: 'name', width: 100 },
    { title: '融合分', dataIndex: 'model_score', width: 90, render: percent },
    { title: '成员1秩', dataIndex: 'member_1_rank_pct', width: 90, render: percent },
    { title: '成员2秩', dataIndex: 'member_2_rank_pct', width: 90, render: percent },
    { title: '信号收盘', dataIndex: 'close', width: 95, render: (value) => fixed(value) },
    { title: '次日收益', dataIndex: 't1_ret', width: 95, render: percent },
    { title: '实际涨跌', dataIndex: 't1_up', width: 90, render: (value) => Number(value) === 1 ? <Tag color="success">上涨</Tag> : <Tag color="error">未上涨</Tag> },
    { title: '组合权重', dataIndex: 'weight', width: 90, render: percent },
    { title: '换手率', dataIndex: 'turnover', width: 90, render: percent },
    { title: '调仓动作', dataIndex: 'rebalance_action', width: 90 },
  ]
  return <Card title="每日 Top15 持仓与下一交易日结果"><Table<Row> size="small" scroll={{ x: 1350 }} pagination={{ pageSize: 15, showSizeChanger: true }} dataSource={records} columns={columns} rowKey={(row) => `${String(row.date)}-${String(row.code)}`} /></Card>
}
