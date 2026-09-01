import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Col, Empty, Row, Space, Spin, Statistic, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { paperTradingApi } from '../../api/paperTradingApi'
import { queryKeys } from '../../api/queryKeys'
import type { GenericRecord } from '../../types/paperTrading'
import { formatValue } from '../common/ValueText'
import { PaperSectionCard } from './PaperSectionCard'

function numberValue(record: GenericRecord, ...keys: string[]): number | null {
  for (const key of keys) {
    const rawValue = record[key]
    if (rawValue === null || rawValue === undefined || rawValue === '') continue
    const value = Number(rawValue)
    if (Number.isFinite(value)) return value
  }
  return null
}

function moneyValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  return Number.isFinite(number)
    ? number.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : '—'
}

function stockCell(record: GenericRecord) {
  return (
    <Space direction="vertical" size={0}>
      <Typography.Text strong>{String(record.stock_code ?? '—')}</Typography.Text>
      <Typography.Text type="secondary">{String(record.stock_name ?? '')}</Typography.Text>
    </Space>
  )
}

const positionColumns: ColumnsType<GenericRecord> = [
  { title: '股票', key: 'stock', fixed: 'left', width: 140, render: (_, record) => stockCell(record) },
  { title: '持仓数量', dataIndex: 'quantity', width: 110, align: 'right', render: formatValue },
  { title: '可用数量', dataIndex: 'available_quantity', width: 110, align: 'right', render: formatValue },
  { title: '平均成本', key: 'average_cost', width: 105, align: 'right', render: (_, record) => moneyValue(numberValue(record, 'average_cost', 'cost_price')) },
  { title: '当日收盘价', key: 'last_price', width: 115, align: 'right', render: (_, record) => moneyValue(numberValue(record, 'last_price', 'current_price')) },
  { title: '持仓市值', dataIndex: 'market_value', width: 125, align: 'right', render: moneyValue },
  {
    title: '仓位',
    key: 'weight',
    width: 100,
    align: 'right',
    render: (_, record) => {
      const value = numberValue(record, 'position_weight', 'position_ratio')
      return value === null ? '—' : `${(value * 100).toFixed(2)}%`
    },
  },
  { title: '浮动盈亏', key: 'profit', width: 120, align: 'right', render: (_, record) => moneyValue(numberValue(record, 'unrealized_profit', 'unrealized_pnl')) },
]

const marketColumns: ColumnsType<GenericRecord> = [
  { title: '开盘', dataIndex: 'open', width: 90, align: 'right', render: moneyValue },
  { title: '最高', dataIndex: 'high', width: 90, align: 'right', render: moneyValue },
  { title: '最低', dataIndex: 'low', width: 90, align: 'right', render: moneyValue },
  { title: '收盘', dataIndex: 'close', width: 90, align: 'right', render: moneyValue },
  {
    title: '行情匹配',
    dataIndex: 'market_data_status',
    width: 100,
    render: (value, record) => <Tag color={record.ohlc_available ? 'success' : record.close_available ? 'processing' : 'warning'}>{String(value || (record.ohlc_available ? '完整OHLC' : record.close_available ? '仅收盘' : '缺失'))}</Tag>,
  },
]

const buyColumns: ColumnsType<GenericRecord> = [
  { title: '批次 ID', dataIndex: 'trade_record_id', fixed: 'left', width: 175, render: formatValue },
  { title: '股票', key: 'stock', fixed: 'left', width: 140, render: (_, record) => stockCell(record) },
  { title: '成交价', dataIndex: 'executed_price', width: 95, align: 'right', render: moneyValue },
  { title: '数量', dataIndex: 'quantity', width: 95, align: 'right', render: formatValue },
  { title: '成交金额', dataIndex: 'gross_amount', width: 120, align: 'right', render: moneyValue },
  { title: '买入费用', dataIndex: 'total_fee', width: 105, align: 'right', render: moneyValue },
  ...marketColumns,
  { title: '操作原因', dataIndex: 'reason', width: 320, ellipsis: true, render: formatValue },
]

const sellColumns: ColumnsType<GenericRecord> = [
  { title: '卖出 ID', dataIndex: 'trade_record_id', fixed: 'left', width: 175, render: formatValue },
  { title: '股票', key: 'stock', fixed: 'left', width: 140, render: (_, record) => stockCell(record) },
  { title: '卖出价', dataIndex: 'executed_price', width: 95, align: 'right', render: moneyValue },
  { title: '数量', dataIndex: 'quantity', width: 95, align: 'right', render: formatValue },
  { title: '成交金额', dataIndex: 'gross_amount', width: 120, align: 'right', render: moneyValue },
  { title: '卖出费用', dataIndex: 'total_fee', width: 105, align: 'right', render: moneyValue },
  {
    title: '买入批次追溯',
    dataIndex: 'purchase_lot_count',
    width: 135,
    render: (value) => Number(value) > 0
      ? <Tag color="blue">匹配 {Number(value)} 个持仓批次</Tag>
      : <Tag color="warning">没有可匹配批次</Tag>,
  },
  ...marketColumns,
  { title: '操作原因', dataIndex: 'reason', width: 320, ellipsis: true, render: formatValue },
]

const purchaseLotColumns: ColumnsType<GenericRecord> = [
  { title: '批次 ID', dataIndex: 'lot_id', width: 175, render: formatValue },
  { title: '买入日期', dataIndex: 'trade_date', width: 115, render: formatValue },
  { title: '股票代码', dataIndex: 'stock_code', width: 105, render: formatValue },
  { title: '买入价格', dataIndex: 'executed_price', width: 100, align: 'right', render: moneyValue },
  { title: '原买入数量', dataIndex: 'original_quantity', width: 110, align: 'right', render: formatValue },
  { title: '本次匹配数量', dataIndex: 'quantity', width: 120, align: 'right', render: formatValue },
  { title: '匹配买入金额', dataIndex: 'gross_amount', width: 120, align: 'right', render: moneyValue },
  { title: '分摊买入费用', dataIndex: 'total_fee', width: 120, align: 'right', render: moneyValue },
]

function positionRowKey(record: GenericRecord, index?: number): string {
  return String(record.order_id ?? record.position_id ?? `${record.stock_code ?? 'row'}-${index ?? 0}`)
}

function tradeRowKey(record: GenericRecord, index?: number): string {
  return String(
    record.trade_record_id
    ?? `${record.trade_date ?? 'date'}_${record.stock_code ?? `row-${index ?? 0}`}`,
  )
}

function purchaseLots(record: GenericRecord): GenericRecord[] {
  return Array.isArray(record.purchase_lots)
    ? record.purchase_lots.filter((item): item is GenericRecord => Boolean(item && typeof item === 'object'))
    : []
}

export function DailyHistoryPanel({ userId, availableDates }: { userId: string; availableDates: string[] }) {
  const dates = useMemo(
    () => Array.from(new Set(availableDates.filter(Boolean))).sort(),
    [availableDates],
  )
  const [selectedDate, setSelectedDate] = useState('')

  useEffect(() => {
    setSelectedDate((current) => (current && dates.includes(current) ? current : (dates.at(-1) ?? '')))
  }, [dates, userId])

  const history = useQuery({
    queryKey: queryKeys.paperHistory(userId, selectedDate),
    queryFn: () => paperTradingApi.history(userId, selectedDate),
    enabled: Boolean(userId && selectedDate),
    staleTime: 60_000,
  })
  const selectedIndex = dates.indexOf(selectedDate)
  const data = history.data

  return (
    <PaperSectionCard sectionKey="daily-history" title="每日操作核验">
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <div className="paper-history-toolbar">
          <Space wrap>
            <Typography.Text strong>核验日期</Typography.Text>
            <input
              type="date"
              className="paper-history-date-picker"
              aria-label="选择模拟盘历史日期"
              value={selectedDate}
              min={dates.at(0)}
              max={dates.at(-1)}
              onChange={(event) => setSelectedDate(event.target.value)}
            />
            <Button
              disabled={selectedIndex <= 0}
              onClick={() => setSelectedDate(dates[selectedIndex - 1])}
            >
              上一交易日
            </Button>
            <Button
              disabled={selectedIndex < 0 || selectedIndex >= dates.length - 1}
              onClick={() => setSelectedDate(dates[selectedIndex + 1])}
            >
              下一交易日
            </Button>
          </Space>
          <Typography.Text type="secondary">每次仅展示一个日期，持仓为该日操作完成后的收盘快照。</Typography.Text>
        </div>

        {dates.length === 0 ? <Empty description="暂无历史持仓快照" /> : null}
        {history.isLoading ? <div className="paper-history-loading"><Spin /></div> : null}
        {history.error ? <Alert type="error" showIcon message="历史记录加载失败" description={String(history.error)} /> : null}

        {data ? (
          <>
            {!data.has_position_snapshot ? (
              <Alert type="warning" showIcon message={`${data.trade_date} 没有独立持仓快照`} description="当日买卖仍会展示；持仓表不会用其他日期的数据代替。" />
            ) : null}
            {data.summary.ohlc_missing_count > 0 ? (
              <Alert type="warning" showIcon message={`${data.summary.ohlc_missing_count} 笔操作缺少完整开高低收行情`} description={`已从主动模型行情缓存匹配 ${data.summary.close_matched_count ?? 0} 笔当日收盘价；开盘、最高和最低仍缺失时以“—”展示，不会用其他交易日行情补齐。`} />
            ) : null}

            <Row gutter={[12, 12]}>
              <Col xs={12} md={6}><Statistic title="收盘持仓" value={data.summary.position_count} suffix="只" /></Col>
              <Col xs={12} md={6}><Statistic title="买入操作" value={data.summary.buy_count} suffix="笔" /></Col>
              <Col xs={12} md={6}><Statistic title="卖出操作" value={data.summary.sell_count} suffix="笔" /></Col>
              <Col xs={12} md={6}><Statistic title="OHLC 已匹配" value={data.summary.ohlc_matched_count} suffix={`/ ${data.summary.operation_count}`} /></Col>
            </Row>

            <div>
              <Typography.Title level={5}>当日买入操作</Typography.Title>
              <Table<GenericRecord>
                size="small"
                scroll={{ x: 1500 }}
                pagination={data.buy_operations.total > 20 ? { pageSize: 20, showSizeChanger: true } : false}
                dataSource={data.buy_operations.records}
                columns={buyColumns}
                rowKey={tradeRowKey}
                locale={{ emptyText: '当日没有实际买入操作' }}
              />
            </div>

            <div>
              <Typography.Title level={5}>当日卖出操作</Typography.Title>
              <Table<GenericRecord>
                size="small"
                scroll={{ x: 1660 }}
                pagination={data.sell_operations.total > 20 ? { pageSize: 20, showSizeChanger: true } : false}
                dataSource={data.sell_operations.records}
                columns={sellColumns}
                rowKey={tradeRowKey}
                locale={{ emptyText: '当日没有实际卖出操作' }}
                expandable={{
                  defaultExpandAllRows: true,
                  rowExpandable: (record) => purchaseLots(record).length > 0,
                  expandedRowRender: (record) => {
                    const lots = purchaseLots(record)
                    return (
                      <Space direction="vertical" size="small" style={{ width: '100%' }}>
                        <Typography.Text strong>
                          本次卖出匹配买入批次：共 {lots.length} 次（以卖出前持仓快照为准，已不在持仓中的旧买入不会显示）
                        </Typography.Text>
                        <Table<GenericRecord>
                          size="small"
                          pagination={false}
                          dataSource={lots}
                          columns={purchaseLotColumns}
                          rowKey={(lot) => String(lot.lot_id)}
                          scroll={{ x: 820 }}
                        />
                      </Space>
                    )
                  },
                }}
              />
            </div>

            <div>
              <Typography.Title level={5}>当日收盘后历史持仓</Typography.Title>
              <Table<GenericRecord>
                size="small"
                scroll={{ x: 920 }}
                pagination={data.positions.total > 20 ? { pageSize: 20, showSizeChanger: true } : false}
                dataSource={data.positions.records}
                columns={positionColumns}
                rowKey={positionRowKey}
                locale={{ emptyText: '当日收盘后为空仓，或没有可用持仓快照' }}
              />
            </div>
          </>
        ) : null}
      </Space>
    </PaperSectionCard>
  )
}
