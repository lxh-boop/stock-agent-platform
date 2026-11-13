import { Button, Col, Collapse, Form, Input, InputNumber, Row, Select, Space, Switch, Typography } from 'antd'
import type { FormInstance } from 'antd'
import type { BacktestCatalog, StudyKind } from '../../types/backtests'
import type { BacktestForm } from '../../utils/backtest'

export const studyLabels: Record<StudyKind, string> = { backtest: '策略回测', analysis: '因子分析', screen: '每日选股' }
const indices = [{ value: 'hs300', label: '沪深300' }, { value: 'csi500', label: '中证500' }, { value: 'sse', label: '上证指数' }]
const opts = (values: string[]) => values.map(value => ({ value, label: value }))
const numeric = (name: string | string[], label: string, min: number, max: number, step = 1) => <Col xs={12} lg={6} key={String(name)}><Form.Item name={name} label={label}><InputNumber min={min} max={max} step={step} style={{ width: '100%' }} /></Form.Item></Col>

function Rules({ name, fields, market = false, max = 10 }: { name: string | string[]; fields: string[]; market?: boolean; max?: number }) {
  return <Form.List name={name}>{(items, { add, remove }) => <Space direction="vertical" style={{ width: '100%' }}>
    {items.map(item => <Space key={item.key} wrap align="baseline">
      {market && <Form.Item name={[item.name, 'index']} rules={[{ required: true }]}><Select style={{ width: 125 }} options={indices} /></Form.Item>}
      <Form.Item name={[item.name, 'field']} rules={[{ required: true }]}><Select showSearch placeholder="字段" style={{ width: 150 }} options={opts(fields)} /></Form.Item>
      <Form.Item name={[item.name, 'op']} rules={[{ required: true }]}><Select style={{ width: 100 }} options={['gt','lt','ge','le','eq','between'].map((value, i) => ({ value, label: ['>','<','≥','≤','=','区间'][i] }))} /></Form.Item>
      <Form.Item name={[item.name, 'value']} rules={[{ required: true }]}><InputNumber placeholder="阈值/下限" /></Form.Item>
      <Form.Item name={[item.name, 'upper']}><InputNumber placeholder="区间上限" /></Form.Item>
      <Button onClick={() => remove(item.name)}>删除</Button>
    </Space>)}
    <Button disabled={items.length >= max} onClick={() => add({ op: 'gt', value: 0, ...(market ? { index: 'sse' } : {}) })}>添加条件</Button>
  </Space>}</Form.List>
}

export function ResearchForm({ form, kind, catalog }: { form: FormInstance<BacktestForm>; kind: StudyKind; catalog?: BacktestCatalog }) {
  const weighting = Form.useWatch(['weighting', 'method'], { form, preserve: true })
  const sizing = Form.useWatch(['sizing', 'method'], { form, preserve: true })
  const timing = Form.useWatch('timing_enabled', { form, preserve: true })
  const fields = catalog?.fields ?? []
  return <Form form={form} layout="vertical" initialValues={{ name: '因子策略', factors: [{ field: 'ROC20', direction: -1, weight: 1 }],
    initial_cash: 1000000, max_positions: 10, rebalance_days: 5, exposure: 1, execution: 'open', buy_fee: .0003, sell_fee: .0008,
    slippage_bps: 0, entry_filters: [], keep_rules: [], sell_filters: [], market_filters: [], exclude_ranges: [],
    weighting: { method: 'fixed', window: 20 }, sizing: { method: 'equal', proportions: [] }, benchmark: 'pool',
    min_position: 0, position_band: .05, keep_invested: false, segment_count: 5, interval_days: 5, decay_days: 5 }}>
    <Row gutter={16}>
      <Col xs={24} lg={8}><Form.Item name="name" label="名称" rules={[{ required: true }]}><Input maxLength={80} /></Form.Item></Col>
      {kind !== 'screen' && <Col xs={12} lg={8}><Form.Item name="start_date" label="开始日期" rules={[{ required: true }]}><Input type="date" min={catalog?.coverage?.start_date} max={catalog?.coverage?.end_date} /></Form.Item></Col>}
      <Col xs={12} lg={8}><Form.Item name="end_date" label={kind === 'screen' ? '查询交易日' : '结束日期'} rules={[{ required: true }]}><Input type="date" min={catalog?.coverage?.start_date} max={catalog?.coverage?.end_date} /></Form.Item></Col>
    </Row>
    <Form.List name="factors">{(items, { add, remove }) => <Space direction="vertical" style={{ width: '100%' }}>
      {items.map(item => <Space key={item.key} wrap align="baseline">
        <Form.Item name={[item.name, 'field']} label="因子" rules={[{ required: true }]}><Select showSearch style={{ width: 160 }} options={opts(fields)} /></Form.Item>
        <Form.Item name={[item.name, 'direction']} label="方向"><Select style={{ width: 130 }} options={[{ value: 1, label: '值大优先' }, { value: -1, label: '值小优先' }]} /></Form.Item>
        <Form.Item name={[item.name, 'weight']} label="固定相对权重"><InputNumber min={0} max={100} disabled={kind === 'analysis' || weighting === 'rank_ic'} /></Form.Item>
        <Button disabled={items.length === 1} onClick={() => remove(item.name)}>删除</Button>
      </Space>)}
      <Button disabled={items.length >= 10} onClick={() => add({ direction: 1, weight: 1 })}>添加因子</Button>
    </Space>}</Form.List>
    {kind === 'analysis' ? <Row gutter={16} style={{ marginTop: 16 }}>
      {numeric('segment_count', '分层数', 2, 10)}{numeric('interval_days', '采样/持有间隔（交易日）', 1, 250)}{numeric('decay_days', 'IC衰减最长持有日', 1, 20)}
      <Typography.Paragraph type="secondary">分层收益不计交易费用；IC/相关性使用方向调整后的平均秩，缺值剔除，不等同账户回测。</Typography.Paragraph>
    </Row> : <>
      <Row gutter={16} style={{ marginTop: 16 }}>
        {kind === 'backtest' && numeric('initial_cash', '初始资金（元）', 10000, 1000000000, 10000)}
        {numeric('max_positions', kind === 'screen' ? '最多选股数' : '最多持仓数', 1, 50)}
        {kind === 'backtest' && numeric('rebalance_days', '调仓间隔（0=条件触发）', 0, 250)}
        {numeric('exposure', '目标总仓位（0～1）', .001, 1, .1)}
      </Row>
      <Collapse items={[
        { key: 'signals', label: '入场过滤、卖出条件与持有保护', children: <>
          <Typography.Paragraph>入场：全部满足；触发模式至少一条。指数条件也必须满足，缺值不放行。</Typography.Paragraph>
          <Rules name="entry_filters" fields={fields} />
          <Typography.Paragraph style={{ marginTop: 16 }}>大盘入场条件</Typography.Paragraph><Rules name="market_filters" fields={catalog?.index_fields ?? []} market max={5} />
          {kind === 'backtest' && <>
            <Typography.Paragraph style={{ marginTop: 16 }}>字段卖出：任一条件满足，下一交易日退出。</Typography.Paragraph><Rules name="sell_filters" fields={fields} max={5} />
            <Typography.Paragraph style={{ marginTop: 16 }}>不卖条件：任一满足则保留、占用持仓名额；不能阻挡择时减仓。</Typography.Paragraph><Rules name="keep_rules" fields={fields} max={5} />
            <Row gutter={16} style={{ marginTop: 16 }}>{numeric('stop_loss', '止损比例（0.05=5%）', .001, .999, .01)}{numeric('take_profit', '止盈比例', .001, 10, .01)}
              {numeric('trailing_stop', '持仓峰值回撤', .001, .999, .01)}{numeric('max_hold_days', '最长持有交易日', 1, 1300)}{numeric('rank_out', '排名跌出前N名时退出', 1, 1000)}</Row>
          </>}
        </> },
        { key: 'weights', label: '动态因子权重与股票仓位分配', children: <>
          <Row gutter={16}>
            <Col xs={12} lg={6}><Form.Item name={['weighting','method']} label="因子评分权重"><Select options={[{ value:'fixed',label:'固定相对权重' },{ value:'rank_ic',label:'滚动 Rank IC 正部' }]} /></Form.Item></Col>
            {weighting === 'rank_ic' && numeric(['weighting','window'], 'IC滚动窗口（交易日）', 5, 250)}
            <Col xs={12} lg={6}><Form.Item name={['sizing','method']} label="股票资金分配"><Select options={[{ value:'equal',label:'按持仓名额等权' },{ value:'fixed',label:'按名次固定比例' },{ value:'factor',label:'指定因子正值比例' }]} /></Form.Item></Col>
            {sizing === 'factor' && <Col xs={12} lg={6}><Form.Item name={['sizing','field']} label="定权因子" rules={[{ required: true }]}><Select showSearch options={opts(fields)} /></Form.Item></Col>}
          </Row>
          {sizing === 'fixed' && <Form.Item name="proportions_text" label="按排名填写比例（逗号分隔；如0.5,0.3,0.2）" rules={[{ required: true }]}><Input /></Form.Item>}
          <Typography.Paragraph type="secondary">动态IC只用已兑现收益；无正IC回退等权。固定比例合计超过1时归一化，填满名额后剩余比例留现金；因子仓位全为非正时不买入。</Typography.Paragraph>
        </> },
        { key: 'market', label: '真实指数基准与大盘择时', children: <>
          <Form.Item name="benchmark" label="回测参考基准"><Select options={[{ value:'pool',label:'同池等权研究参考（不是指数）' },...indices]} /></Form.Item>
          <Form.Item name="timing_enabled" label="启用指数择时" valuePropName="checked"><Switch /></Form.Item>
          {timing && <>
            <Typography.Paragraph>条件成立投牛票，不成立投熊票，缺值弃权；昨日状态决定今日仓位。</Typography.Paragraph>
            <Rules name={['timing','rules']} fields={catalog?.index_fields ?? []} market max={3} />
            <Row gutter={16} style={{ marginTop: 16 }}>{numeric(['timing','enter_votes'], '熊转牛票数（-1=全部）', -1, 3)}{numeric(['timing','exit_votes'], '牛转熊票数（-1=全部）', -1, 3)}{numeric(['timing','bear_exposure'], '熊市保留比例（0～1）', 0, 1, .1)}</Row>
          </>}
          <Typography.Paragraph type="secondary">真实指数首次通过本人的 Tushare 拉取并缓存。权限不足/缺数据报错，不替换成模拟基准。ret5/ret20为小数；pct_chg为百分数；close_ma20为价格/均线比。</Typography.Paragraph>
        </> },
        ...(kind === 'backtest' ? [{ key: 'execution', label: '成交假设、费用与暂停交易区间', children: <>
          <Row gutter={16}>
            <Col xs={12} lg={6}><Form.Item name="execution" label="次日成交时点"><Select options={[{value:'open',label:'开盘'}, {value:'close',label:'收盘'}]} /></Form.Item></Col>
            {numeric('buy_fee','买入综合费率（小数）',0,.02,.0001)}{numeric('sell_fee','卖出综合费率（小数）',0,.02,.0001)}{numeric('slippage_bps','单边滑点（基点）',0,100)}
            {numeric('min_position','最小建仓比例',0,1,.01)}{numeric('position_band','仓位偏离容忍比例',0,.5,.01)}{numeric('limit_pct','固定涨跌幅限制（留空关闭）',.01,.5,.01)}
            <Col xs={12} lg={6}><Form.Item name="keep_invested" label="日常维持目标仓位" valuePropName="checked"><Switch /></Form.Item></Col>
          </Row>
          <Typography.Paragraph type="secondary">涨跌幅为研究近似，不自动识别不同板块/ST/上市首日。所有买卖统一校验。暂停只停止交易，持仓仍按市场估值，不冻结净值。</Typography.Paragraph>
          <Form.List name="exclude_ranges">{(items,{add,remove}) => <Space direction="vertical">{items.map(item => <Space key={item.key} align="baseline">
            <Form.Item name={[item.name,'start']} rules={[{required:true}]}><Input type="date" /></Form.Item><Form.Item name={[item.name,'end']} rules={[{required:true}]}><Input type="date" /></Form.Item><Button onClick={()=>remove(item.name)}>删除区间</Button>
          </Space>)}<Button disabled={items.length>=5} onClick={()=>add()}>添加暂停区间</Button></Space>}</Form.List>
        </> }] : []),
      ]} />
    </>}
    <Collapse style={{ marginTop: 16 }} items={[{ key:'pool',label:'股票池与历史成员快照',children:<>
      <Form.Item name="codes" label="六位代码，逗号分隔；留空使用个人库，最多1000只"><Input.TextArea rows={2} /></Form.Item>
      <Form.Item name="pool_schedule_text" label="可选：历史成员快照 JSON，不使用未来快照"><Input.TextArea rows={4} placeholder={'[{"as_of":"2025-01-01","stock_codes":["000001","000002"]}]'} /></Form.Item>
      <Typography.Paragraph type="secondary">只使用不晚于信号日的最后一份快照。未提供时按个人库股票回测，不宣称重建历史指数/行业成分。</Typography.Paragraph>
    </> }]} />
  </Form>
}
