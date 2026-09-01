import { Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { RankingRecord } from '../../types/dashboard'

const finite = (value: unknown) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
const price = (value: unknown) => finite(value) === null ? '—' : finite(value)!.toFixed(2)
const score = (value: unknown) => finite(value) === null ? '—' : (finite(value)! * 100).toFixed(2)
const historicalRate = (value: unknown) => finite(value) !== null
  ? <Tooltip title="独立留出集中相同排名分组的下一交易日实际上涨率；不是该股票的个体预测概率"><span>{(finite(value)! * 100).toFixed(2)}%</span></Tooltip>
  : '—'

export function RankingTable({ records, onSelect }: { records: RankingRecord[]; onSelect?: (code: string) => void }) {
  const columns: ColumnsType<RankingRecord> = [
    { title: '排名', dataIndex: 'rank', width: 70, fixed: 'left', render: (value, _row, index) => value ?? index + 1 },
    { title: '股票代码', dataIndex: 'code', width: 105, fixed: 'left', render: (value) => <a onClick={() => onSelect?.(String(value))}>{String(value ?? '')}</a> },
    { title: '股票名称', dataIndex: 'name', width: 115, fixed: 'left' },
    { title: '融合排序分', dataIndex: 'model_score', width: 120, render: score },
    { title: '成员1截面秩', dataIndex: 'member_1_rank_pct', width: 130, render: score },
    { title: '成员2截面秩', dataIndex: 'member_2_rank_pct', width: 130, render: score },
    { title: '历史同排名组上涨率', dataIndex: 'historical_rank_bucket_up_rate', width: 175, render: historicalRate },
    { title: '当前收盘', dataIndex: 'close', width: 105, render: price },
    { title: '预测日期', dataIndex: 'prediction_date', width: 125, render: (value) => <Tag color="blue">{String(value ?? '—').slice(0, 10)}</Tag> },
    { title: '信号日期', dataIndex: 'date', width: 125, render: (value, row) => <Tag color={row.ohlc_available ? 'default' : 'warning'}>{String(value ?? '—').slice(0, 10)}</Tag> },
  ]
  return <Table<RankingRecord> size="middle" scroll={{ x: 1150 }} pagination={{ pageSize: 20, showSizeChanger: true }} dataSource={records} columns={columns} rowKey={(row) => `${String(row.code ?? '')}-${String(row.rank ?? '')}-${String(row.name ?? '')}`} />
}
