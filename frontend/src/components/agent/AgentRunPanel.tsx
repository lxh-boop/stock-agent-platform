import { Collapse, Descriptions, Empty, List, Space, Spin, Table, Tag } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { agentApi } from '../../api/agentApi'
import type { AgentRuntimeCall } from '../../types/agent'

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="agent-json-block">{JSON.stringify(value ?? {}, null, 2)}</pre>
}

function statusColor(status: string) {
  if (['success', 'succeeded', 'completed'].includes(status)) return 'success'
  if (['failed', 'error'].includes(status)) return 'error'
  if (['running', 'pending', 'ready'].includes(status)) return 'processing'
  return 'default'
}

function durationText(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '-'
  return value < 1 ? `${Math.round(value * 1000)} ms` : `${value.toFixed(2)} s`
}

export function AgentRunPanel({ userId, runId }: { userId: string; runId: string }) {
  const run = useQuery({
    queryKey: ['web', 'agent', 'run', userId, runId],
    queryFn: () => agentApi.run(userId, runId),
    enabled: Boolean(runId),
  })
  if (!runId) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择一条包含 run_id 的回答查看运行详情" />
  if (run.isLoading) return <Spin />

  const data = run.data
  const runInfo = data?.run ?? {}
  const steps = data?.steps ?? []
  const tools = data?.tool_calls ?? []
  const sources = data?.sources ?? []

  return <Space direction="vertical" size="middle" style={{ width: '100%' }} data-testid="agent-run-details">
    <Descriptions bordered size="small" column={2}>
      <Descriptions.Item label="Run ID">{String(runInfo.run_id ?? runId)}</Descriptions.Item>
      <Descriptions.Item label="状态"><Tag>{String(runInfo.status ?? '-')}</Tag></Descriptions.Item>
      <Descriptions.Item label="目标" span={2}>{String(runInfo.goal ?? '-')}</Descriptions.Item>
      <Descriptions.Item label="步骤">{data?.counts.steps ?? steps.length}</Descriptions.Item>
      <Descriptions.Item label="工具 / Worker 调用">{data?.counts.tool_calls ?? tools.length}</Descriptions.Item>
    </Descriptions>
    <Collapse
      defaultActiveKey={['steps', 'tools']}
      items={[
        {
          key: 'steps',
          label: `计划与步骤（${steps.length}）`,
          children: <Table
            size="small"
            rowKey={(row) => String(row.step_id ?? `${row.intent ?? 'step'}-${row.status ?? 'unknown'}`)}
            dataSource={steps}
            pagination={false}
            columns={[
              { title: '步骤', dataIndex: 'step_id', width: 150 },
              { title: '意图', dataIndex: 'intent' },
              { title: '状态', dataIndex: 'status', width: 110 },
              { title: '观察摘要', dataIndex: 'observation' },
            ]}
          />,
        },
        {
          key: 'tools',
          label: `工具 / Worker 调用（${tools.length}）`,
          children: <Table<AgentRuntimeCall>
            size="small"
            rowKey={(row) => row.tool_call_id}
            dataSource={tools}
            pagination={false}
            locale={{ emptyText: '本次运行没有可展示的工具或 Worker 调用' }}
            expandable={{
              expandedRowRender: (row) => <Descriptions bordered size="small" column={1}>
                <Descriptions.Item label="输入摘要"><JsonBlock value={row.input_summary} /></Descriptions.Item>
                <Descriptions.Item label="输出摘要"><JsonBlock value={row.output_summary} /></Descriptions.Item>
                {row.error_message
                  ? <Descriptions.Item label="错误信息">{row.error_message}</Descriptions.Item>
                  : null}
              </Descriptions>,
              rowExpandable: (row) => Boolean(
                row.input_summary || row.output_summary || row.error_message
              ),
            }}
            columns={[
              {
                title: '类型',
                dataIndex: 'call_kind',
                width: 90,
                render: (value) => value === 'worker'
                  ? <Tag color="purple">Worker</Tag>
                  : <Tag color="blue">Tool</Tag>,
              },
              { title: '工具', dataIndex: 'tool_name' },
              { title: '步骤', dataIndex: 'step_id' },
              {
                title: '状态',
                dataIndex: 'status',
                width: 110,
                render: (value) => <Tag color={statusColor(String(value ?? ''))}>
                  {String(value ?? '-')}
                </Tag>,
              },
              {
                title: '耗时',
                dataIndex: 'duration_seconds',
                width: 100,
                render: (value) => durationText(Number(value)),
              },
              {
                title: '错误',
                dataIndex: 'error_type',
                render: (value) => String(value || '-'),
              },
            ]}
          />,
        },
        {
          key: 'sources',
          label: `证据来源（${sources.length}）`,
          children: <List
            dataSource={sources}
            locale={{ emptyText: '暂无证据来源' }}
            renderItem={(item) => <List.Item>
              <List.Item.Meta
                title={`${String(item.source_type ?? '')} · ${String(item.title ?? '')}`}
                description={String(item.snippet ?? '')}
              />
            </List.Item>}
          />,
        },
      ]}
    />
  </Space>
}
