import { Col, Row, Statistic, Tag, Typography } from 'antd'
import { PaperSectionCard } from './PaperSectionCard'

type Account = Record<string, unknown>

function numberValue(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function first(account: Account, keys: string[]): unknown {
  return keys.map((key) => account[key]).find((value) => value !== undefined && value !== null)
}

export function AccountSummary({ account, available }: { account: Account; available: boolean }) {
  const items = [
    ['总资产', ['total_assets', 'total_asset', 'equity']],
    ['现金', ['cash', 'cash_balance', 'available_cash']],
    ['持仓市值', ['position_market_value', 'market_value', 'positions_value']],
    ['累计收益', ['total_return', 'cumulative_return', 'return_rate']],
  ] as const
  return (
    <PaperSectionCard sectionKey="account-summary" title="账户摘要" extra={<Tag color={available ? 'success' : 'warning'}>{available ? '数据可用' : '暂无账户快照'}</Tag>}>
      <Row gutter={[16, 16]}>
        {items.map(([label, keys]) => {
          const value = first(account, [...keys])
          const isReturn = label.includes('收益')
          return <Col xs={12} xl={6} key={label}><Statistic title={label} value={isReturn ? numberValue(value) * 100 : numberValue(value)} precision={2} suffix={isReturn ? '%' : ' 元'} /></Col>
        })}
      </Row>
      {Object.keys(account).length === 0 ? <Typography.Text type="secondary">尚未建立模拟盘账户。</Typography.Text> : null}
    </PaperSectionCard>
  )
}
